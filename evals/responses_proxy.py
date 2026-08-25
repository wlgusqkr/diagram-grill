#!/usr/bin/env python3
"""A narrow, credential-holding reverse proxy for the fixed Codex evaluation.

The proxy accepts only the streaming Responses API request shape used by the
evaluation.  Credentials are read once from stdin and are never included in
application logs, error messages, or downstream responses.
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import json
import logging
import re
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, BinaryIO, Callable, Iterable, List, Mapping, Optional, Sequence, Tuple


UPSTREAM_HOST = "api.openai.com"
UPSTREAM_PORT = 443
UPSTREAM_PATH = "/v1/responses"
MAX_CREDENTIAL_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_TOKENS = 16_000
MAX_REQUESTS = 24
TTL_SECONDS = 1_200
IO_CHUNK_BYTES = 64 * 1024
CAPABILITY_HEADER = "X-Diagram-Grill-Eval-Capability"
CODEX_REQUEST_SCHEMA_VERSION = "0.149.1"
ALLOWED_TOOL_TYPES = frozenset(("function", "custom"))
FORBIDDEN_INBOUND_HEADERS = frozenset(
    ("authorization", "cookie", "proxy-authorization")
)
_CAPABILITY_HEADER = CAPABILITY_HEADER.lower()
# Frozen from Codex CLI 0.149.1's ResponsesApiRequest wire struct. New Codex
# request fields fail closed here until the evaluator pin and this policy move
# together. ``max_output_tokens`` is the one proxy policy extension: 0.149.1
# does not emit it, and the proxy always inserts or caps it. JSON Schema bodies
# and text are the only intentionally opaque data.
_TOP_LEVEL_FIELDS = frozenset(
    (
        "client_metadata",
        "include",
        "input",
        "instructions",
        "max_output_tokens",
        "model",
        "parallel_tool_calls",
        "prompt_cache_key",
        "reasoning",
        "service_tier",
        "store",
        "stream",
        "stream_options",
        "text",
        "tool_choice",
        "tools",
    )
)
_REASONING_FIELDS = frozenset(("context", "effort", "summary"))
_STREAM_OPTION_FIELDS = frozenset(("reasoning_summary_delivery",))
_TEXT_FIELDS = frozenset(("format", "verbosity"))
_TEXT_FORMAT_FIELDS = frozenset(("name", "schema", "strict", "type"))
_FUNCTION_TOOL_FIELDS = frozenset(
    ("defer_loading", "description", "name", "parameters", "strict", "type")
)
_CUSTOM_TOOL_FIELDS = frozenset(
    ("defer_loading", "description", "format", "name", "type")
)
_CUSTOM_TOOL_FORMAT_FIELDS = frozenset(("definition", "syntax", "type"))
_INPUT_ITEM_FIELDS = {
    "compaction": frozenset(
        ("encrypted_content", "id", "internal_chat_message_metadata_passthrough", "type")
    ),
    "compaction_trigger": frozenset(("type",)),
    "context_compaction": frozenset(
        ("encrypted_content", "id", "internal_chat_message_metadata_passthrough", "type")
    ),
    "custom_tool_call": frozenset(
        (
            "call_id",
            "id",
            "input",
            "internal_chat_message_metadata_passthrough",
            "name",
            "status",
            "type",
        )
    ),
    "custom_tool_call_output": frozenset(
        (
            "call_id",
            "id",
            "internal_chat_message_metadata_passthrough",
            "name",
            "output",
            "type",
        )
    ),
    "function_call": frozenset(
        (
            "arguments",
            "call_id",
            "encrypted_function_args",
            "id",
            "internal_chat_message_metadata_passthrough",
            "name",
            "type",
        )
    ),
    "function_call_output": frozenset(
        (
            "call_id",
            "id",
            "internal_chat_message_metadata_passthrough",
            "output",
            "type",
        )
    ),
    "local_shell_call": frozenset(
        (
            "action",
            "call_id",
            "id",
            "internal_chat_message_metadata_passthrough",
            "status",
            "type",
        )
    ),
    "message": frozenset(
        (
            "content",
            "id",
            "internal_chat_message_metadata_passthrough",
            "phase",
            "role",
            "type",
        )
    ),
    "reasoning": frozenset(
        (
            "content",
            "encrypted_content",
            "id",
            "internal_chat_message_metadata_passthrough",
            "summary",
            "type",
        )
    ),
}
_METADATA_FIELDS = frozenset(("create_time", "executed_tool_calls", "turn_id"))
_LOCAL_SHELL_ACTION_FIELDS = frozenset(
    ("command", "env", "timeout_ms", "type", "user", "working_directory")
)
_FORBIDDEN_REFERENCE_KEYS = frozenset(
    (
        "audio_url",
        "container_id",
        "file",
        "file_data",
        "file_id",
        "file_path",
        "file_uri",
        "file_url",
        "filename",
        "image_url",
        "local_path",
        "path",
        "project",
        "project_id",
        "project_resource",
        "project_resource_id",
        "resource",
        "resource_id",
        "resource_uri",
        "resource_url",
        "uri",
        "url",
        "vector_store_id",
    )
)
_FORBIDDEN_REFERENCE_KEY_SUFFIXES = (
    "_file_id",
    "_project_id",
    "_resource_id",
    "_uri",
    "_url",
)
_FREE_TEXT_REFERENCE_FIELDS = frozenset(
    (
        "arguments",
        "definition",
        "description",
        "encrypted_content",
        "encrypted_function_args",
        "input",
        "instructions",
        "output",
        "text",
    )
)
_OPAQUE_REFERENCE_CONTAINERS = frozenset(
    ("env", "internal_chat_message_metadata_passthrough")
)
_EXTERNAL_REFERENCE = re.compile(
    r"^(?://|(?:https?|ftp|file|data|s3|gs|azure|openai):)", re.IGNORECASE
)
_RESOURCE_IDENTIFIER = re.compile(
    r"^(?:file|proj(?:ect)?|resource|vector[_-]?store|vs|container)[_-][A-Za-z0-9]",
    re.IGNORECASE,
)
_CONTENT_LENGTH = re.compile(r"^[0-9]+$")
_ABSOLUTE_TARGET = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


class CredentialError(ValueError):
    """Raised when the stdin credential envelope is not safe to use."""


class PolicyError(ValueError):
    """A fixed, non-reflective client error."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


class ResponseLimitError(RuntimeError):
    """Raised after an upstream body violates a streaming response policy."""


@dataclass(frozen=True, repr=False)
class CredentialEnvelope:
    api_key: str
    sentinel: str
    capability: str

    def __repr__(self) -> str:
        return (
            "CredentialEnvelope(api_key=<redacted>, sentinel=<redacted>, "
            "capability=<redacted>)"
        )

    @property
    def secret_bytes(self) -> Tuple[bytes, bytes, bytes]:
        return (
            self.api_key.encode("utf-8"),
            self.sentinel.encode("utf-8"),
            self.capability.encode("utf-8"),
        )


def read_credential_envelope(stream: Any) -> CredentialEnvelope:
    """Read exactly one bounded JSON object from a text or binary stream."""

    source = getattr(stream, "buffer", stream)
    data = source.read(MAX_CREDENTIAL_BYTES + 1)
    if not isinstance(data, (bytes, str)):
        raise CredentialError("credential envelope is unreadable")
    encoded_size = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
    if encoded_size > MAX_CREDENTIAL_BYTES:
        raise CredentialError("credential envelope exceeds the size limit")
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CredentialError("credential envelope is not valid JSON") from None
    if not isinstance(value, dict) or set(value) != {
        "api_key",
        "sentinel",
        "capability",
    }:
        raise CredentialError("credential envelope has an invalid shape")
    api_key = value.get("api_key")
    sentinel = value.get("sentinel")
    capability = value.get("capability")
    if (
        not isinstance(api_key, str)
        or not api_key
        or len(api_key) > 8192
        or any(ord(character) < 33 or ord(character) > 126 for character in api_key)
    ):
        raise CredentialError("credential envelope has an invalid api_key")
    if not isinstance(sentinel, str) or len(sentinel) < 16 or len(sentinel) > 8192:
        raise CredentialError("credential envelope has an invalid sentinel")
    if (
        not isinstance(capability, str)
        or len(capability) < 32
        or len(capability) > 256
        or any(ord(character) < 33 or ord(character) > 126 for character in capability)
    ):
        raise CredentialError("credential envelope has an invalid capability")
    if len({api_key, sentinel, capability}) != 3:
        raise CredentialError("credential envelope values must be distinct")
    return CredentialEnvelope(
        api_key=api_key, sentinel=sentinel, capability=capability
    )


def _json_error(code: str) -> bytes:
    return json.dumps(
        {"error": {"code": code, "message": "request rejected by evaluation proxy"}},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _header_items(headers: Any) -> List[Tuple[str, str]]:
    if hasattr(headers, "raw_items"):
        items = list(headers.raw_items())
    elif isinstance(headers, Mapping):
        items = list(headers.items())
    else:
        items = list(headers)
    normalized: List[Tuple[str, str]] = []
    for name, value in items:
        normalized.append((str(name).strip().lower(), str(value).strip()))
    return normalized


def _header_values(headers: Sequence[Tuple[str, str]], name: str) -> List[str]:
    lowered = name.lower()
    return [value for key, value in headers if key == lowered]


def _is_absolute_target(target: str) -> bool:
    if target.startswith("//"):
        return True
    return _ABSOLUTE_TARGET.match(target) is not None


def _contains_secret(value: Any, secrets: Sequence[str]) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in secrets)
    if isinstance(value, list):
        return any(_contains_secret(item, secrets) for item in value)
    if isinstance(value, dict):
        return any(
            _contains_secret(key, secrets) or _contains_secret(item, secrets)
            for key, item in value.items()
        )
    return False


def _reject_unknown_fields(value: Mapping[str, Any], allowed: frozenset) -> None:
    if set(value).difference(allowed):
        raise PolicyError(400, "unknown_request_field")


def _is_reference_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized in _FORBIDDEN_REFERENCE_KEYS or normalized.endswith(
        _FORBIDDEN_REFERENCE_KEY_SUFFIXES
    )


def _reject_resource_references(value: Any, parent_key: Optional[str] = None) -> None:
    """Reject model-provider fetch references while leaving ordinary text opaque."""

    normalized_parent = (
        parent_key.strip().lower().replace("-", "_") if parent_key else None
    )
    if normalized_parent in _OPAQUE_REFERENCE_CONTAINERS:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_reference_key(key):
                raise PolicyError(400, "resource_reference_rejected")
            _reject_resource_references(item, key)
        return
    if isinstance(value, list):
        for item in value:
            _reject_resource_references(item, parent_key)
        return
    if (
        isinstance(value, str)
        and normalized_parent not in _FREE_TEXT_REFERENCE_FIELDS
        and (
            _EXTERNAL_REFERENCE.match(value.strip()) is not None
            or _RESOURCE_IDENTIFIER.match(value.strip()) is not None
        )
    ):
        raise PolicyError(400, "resource_reference_rejected")


def _validate_metadata(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise PolicyError(400, "invalid_request_shape")
    _reject_unknown_fields(value, _METADATA_FIELDS)
    if "turn_id" in value and not isinstance(value["turn_id"], str):
        raise PolicyError(400, "invalid_request_shape")
    if "create_time" in value and not isinstance(value["create_time"], (int, float)):
        raise PolicyError(400, "invalid_request_shape")
    if "executed_tool_calls" in value and not isinstance(
        value["executed_tool_calls"], list
    ):
        raise PolicyError(400, "invalid_request_shape")


def _validate_content_items(value: Any, allowed_types: frozenset) -> None:
    if not isinstance(value, list):
        raise PolicyError(400, "invalid_request_shape")
    for item in value:
        if not isinstance(item, dict):
            raise PolicyError(400, "invalid_request_shape")
        item_type = item.get("type")
        if item_type not in allowed_types:
            raise PolicyError(400, "resource_reference_rejected")
        content_field = "encrypted_content" if item_type == "encrypted_content" else "text"
        _reject_unknown_fields(item, frozenset((content_field, "type")))
        if not isinstance(item.get(content_field), str):
            raise PolicyError(400, "invalid_request_shape")


def _validate_output(value: Any) -> None:
    if isinstance(value, str):
        return
    _validate_content_items(value, frozenset(("encrypted_content", "input_text")))


def _validate_input_item(item: Any) -> None:
    if not isinstance(item, dict):
        raise PolicyError(400, "invalid_request_shape")
    item_type = item.get("type")
    allowed_fields = _INPUT_ITEM_FIELDS.get(item_type)
    if allowed_fields is None:
        raise PolicyError(400, "input_item_type_rejected")
    _reject_unknown_fields(item, allowed_fields)

    metadata = item.get("internal_chat_message_metadata_passthrough")
    _validate_metadata(metadata)

    if item_type == "message":
        if item.get("role") not in ("assistant", "developer", "system", "user"):
            raise PolicyError(400, "invalid_request_shape")
        _validate_content_items(
            item.get("content"), frozenset(("input_text", "output_text"))
        )
        if "phase" in item and item["phase"] not in (
            None,
            "commentary",
            "final_answer",
        ):
            raise PolicyError(400, "invalid_request_shape")
    elif item_type == "reasoning":
        summary = item.get("summary")
        if not isinstance(summary, list):
            raise PolicyError(400, "invalid_request_shape")
        for part in summary:
            if not isinstance(part, dict):
                raise PolicyError(400, "invalid_request_shape")
            _reject_unknown_fields(part, frozenset(("text", "type")))
            if part.get("type") != "summary_text" or not isinstance(
                part.get("text"), str
            ):
                raise PolicyError(400, "invalid_request_shape")
        content = item.get("content")
        if content is not None:
            _validate_content_items(content, frozenset(("reasoning_text", "text")))
        encrypted = item.get("encrypted_content")
        if encrypted is not None and not isinstance(encrypted, str):
            raise PolicyError(400, "invalid_request_shape")
    elif item_type == "function_call":
        for key in ("arguments", "call_id", "name"):
            if not isinstance(item.get(key), str):
                raise PolicyError(400, "invalid_request_shape")
        encrypted_arguments = item.get("encrypted_function_args")
        if encrypted_arguments is not None and (
            not isinstance(encrypted_arguments, list)
            or not all(isinstance(part, str) for part in encrypted_arguments)
        ):
            raise PolicyError(400, "invalid_request_shape")
    elif item_type in ("function_call_output", "custom_tool_call_output"):
        if not isinstance(item.get("call_id"), str):
            raise PolicyError(400, "invalid_request_shape")
        _validate_output(item.get("output"))
        if "name" in item and item["name"] is not None and not isinstance(
            item["name"], str
        ):
            raise PolicyError(400, "invalid_request_shape")
    elif item_type == "custom_tool_call":
        for key in ("call_id", "input", "name"):
            if not isinstance(item.get(key), str):
                raise PolicyError(400, "invalid_request_shape")
        if "status" in item and item["status"] is not None and not isinstance(
            item["status"], str
        ):
            raise PolicyError(400, "invalid_request_shape")
    elif item_type == "local_shell_call":
        if item.get("status") not in ("completed", "in_progress", "incomplete"):
            raise PolicyError(400, "invalid_request_shape")
        action = item.get("action")
        if not isinstance(action, dict):
            raise PolicyError(400, "invalid_request_shape")
        _reject_unknown_fields(action, _LOCAL_SHELL_ACTION_FIELDS)
        if action.get("type") != "exec" or not isinstance(action.get("command"), list):
            raise PolicyError(400, "invalid_request_shape")
        if not all(isinstance(part, str) for part in action["command"]):
            raise PolicyError(400, "invalid_request_shape")
        environment = action.get("env")
        if environment is not None and (
            not isinstance(environment, dict)
            or not all(
                isinstance(key, str) and isinstance(item, str)
                for key, item in environment.items()
            )
        ):
            raise PolicyError(400, "invalid_request_shape")
    elif item_type in ("compaction", "context_compaction"):
        encrypted = item.get("encrypted_content")
        if encrypted is not None and not isinstance(encrypted, str):
            raise PolicyError(400, "invalid_request_shape")


def _validate_tools(tools: Any) -> None:
    if not isinstance(tools, list):
        raise PolicyError(400, "invalid_tools")
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") not in ALLOWED_TOOL_TYPES:
            raise PolicyError(400, "tool_type_rejected")
        if tool["type"] == "function":
            _reject_unknown_fields(tool, _FUNCTION_TOOL_FIELDS)
            if (
                not isinstance(tool.get("name"), str)
                or not isinstance(tool.get("description"), str)
                or not isinstance(tool.get("strict"), bool)
                or not isinstance(tool.get("parameters"), dict)
            ):
                raise PolicyError(400, "invalid_tools")
        else:
            _reject_unknown_fields(tool, _CUSTOM_TOOL_FIELDS)
            if not isinstance(tool.get("name"), str) or not isinstance(
                tool.get("description"), str
            ):
                raise PolicyError(400, "invalid_tools")
            tool_format = tool.get("format")
            if not isinstance(tool_format, dict):
                raise PolicyError(400, "invalid_tools")
            _reject_unknown_fields(tool_format, _CUSTOM_TOOL_FORMAT_FIELDS)
            if set(tool_format) != _CUSTOM_TOOL_FORMAT_FIELDS or not all(
                isinstance(tool_format[key], str) for key in _CUSTOM_TOOL_FORMAT_FIELDS
            ):
                raise PolicyError(400, "invalid_tools")
        if "defer_loading" in tool and not isinstance(tool["defer_loading"], bool):
            raise PolicyError(400, "invalid_tools")


def _validate_request_shape(payload: Mapping[str, Any]) -> None:
    _reject_unknown_fields(payload, _TOP_LEVEL_FIELDS)
    required = frozenset(
        (
            "include",
            "input",
            "parallel_tool_calls",
            "store",
            "tool_choice",
        )
    )
    if required.difference(payload):
        raise PolicyError(400, "invalid_request_shape")
    if "instructions" in payload and not isinstance(payload["instructions"], str):
        raise PolicyError(400, "invalid_request_shape")
    if payload["tool_choice"] != "auto":
        raise PolicyError(400, "invalid_request_shape")
    if not isinstance(payload["parallel_tool_calls"], bool) or not isinstance(
        payload["store"], bool
    ):
        raise PolicyError(400, "invalid_request_shape")
    if payload["include"] != ["reasoning.encrypted_content"]:
        raise PolicyError(400, "invalid_request_shape")

    input_items = payload["input"]
    if not isinstance(input_items, list) or not input_items:
        raise PolicyError(400, "invalid_request_shape")
    _reject_resource_references(input_items)
    for item in input_items:
        _validate_input_item(item)

    _validate_tools(payload.get("tools", []))

    reasoning = payload["reasoning"]
    _reject_unknown_fields(reasoning, _REASONING_FIELDS)
    if "summary" in reasoning and reasoning["summary"] not in (None, "auto"):
        raise PolicyError(400, "invalid_request_shape")
    if "context" in reasoning and reasoning["context"] not in (
        None,
        "all_turns",
        "current_turn",
    ):
        raise PolicyError(400, "invalid_request_shape")

    stream_options = payload.get("stream_options")
    if stream_options is not None:
        if not isinstance(stream_options, dict):
            raise PolicyError(400, "invalid_request_shape")
        _reject_unknown_fields(stream_options, _STREAM_OPTION_FIELDS)
        if stream_options.get("reasoning_summary_delivery") != "sequential_cutoff":
            raise PolicyError(400, "invalid_request_shape")

    text = payload.get("text")
    if text is not None:
        if not isinstance(text, dict):
            raise PolicyError(400, "invalid_request_shape")
        _reject_unknown_fields(text, _TEXT_FIELDS)
        if "verbosity" in text and text["verbosity"] not in ("high", "low", "medium"):
            raise PolicyError(400, "invalid_request_shape")
        text_format = text.get("format")
        if text_format is not None:
            if not isinstance(text_format, dict):
                raise PolicyError(400, "invalid_request_shape")
            _reject_unknown_fields(text_format, _TEXT_FORMAT_FIELDS)
            if (
                set(text_format) != _TEXT_FORMAT_FIELDS
                or text_format.get("type") != "json_schema"
                or not isinstance(text_format.get("strict"), bool)
                or not isinstance(text_format.get("name"), str)
                or not isinstance(text_format.get("schema"), dict)
            ):
                raise PolicyError(400, "invalid_request_shape")

    prompt_cache_key = payload.get("prompt_cache_key")
    if prompt_cache_key is not None and (
        not isinstance(prompt_cache_key, str) or len(prompt_cache_key) > 1024
    ):
        raise PolicyError(400, "invalid_request_shape")
    client_metadata = payload.get("client_metadata")
    if client_metadata is not None and (
        not isinstance(client_metadata, dict)
        or len(client_metadata) > 64
        or not all(
            isinstance(key, str)
            and len(key) <= 128
            and isinstance(value, str)
            and len(value) <= 64 * 1024
            for key, value in client_metadata.items()
        )
    ):
        raise PolicyError(400, "invalid_request_shape")


def _sanitize_payload(
    raw_body: bytes, expected_model: str, credentials: CredentialEnvelope
) -> bytes:
    def reject_nonfinite(_: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        payload = json.loads(
            raw_body.decode("utf-8"), parse_constant=reject_nonfinite
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise PolicyError(400, "invalid_json") from None
    if not isinstance(payload, dict):
        raise PolicyError(400, "json_object_required")
    try:
        if _contains_secret(
            payload,
            (credentials.api_key, credentials.sentinel, credentials.capability),
        ):
            raise PolicyError(400, "credential_material_rejected")
    except RecursionError:
        raise PolicyError(400, "invalid_json") from None
    if payload.get("model") != expected_model:
        raise PolicyError(400, "model_mismatch")
    if payload.get("stream") is not True:
        raise PolicyError(400, "stream_required")

    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, dict) or reasoning.get("effort") != "medium":
        raise PolicyError(400, "medium_reasoning_required")
    if payload.get("service_tier") != "default":
        raise PolicyError(400, "default_service_tier_required")

    _validate_request_shape(payload)

    requested_tokens = payload.get("max_output_tokens")
    if requested_tokens is None:
        payload["max_output_tokens"] = MAX_OUTPUT_TOKENS
    elif (
        not isinstance(requested_tokens, int)
        or isinstance(requested_tokens, bool)
        or requested_tokens <= 0
    ):
        raise PolicyError(400, "invalid_max_output_tokens")
    else:
        payload["max_output_tokens"] = min(requested_tokens, MAX_OUTPUT_TOKENS)
    payload["store"] = False
    try:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError, RecursionError):
        raise PolicyError(400, "invalid_json") from None


class RequestGate:
    """Serialize and bound the requests that may reach the upstream API."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        max_requests: int = MAX_REQUESTS,
        ttl_seconds: int = TTL_SECONDS,
    ) -> None:
        self._clock = clock
        self._deadline = clock() + ttl_seconds
        self._max_requests = max_requests
        self._count = 0
        self._lock = threading.Lock()

    @property
    def deadline(self) -> float:
        return self._deadline

    def active(self) -> bool:
        return self._clock() < self._deadline

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def reserve(self) -> int:
        with self._lock:
            if not self.active():
                raise PolicyError(503, "proxy_expired")
            if self._count >= self._max_requests:
                raise PolicyError(429, "request_limit_reached")
            self._count += 1
            return self._count


class HTTPUpstreamResponse:
    """Tie an HTTPResponse lifecycle to the connection that produced it."""

    def __init__(self, response: Any, connection: Any) -> None:
        self.status = response.status
        self.reason = response.reason
        self.headers = list(response.getheaders())
        self._response = response
        self._connection = connection

    def read(self, amount: int) -> bytes:
        return self._response.read(amount)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


class OpenAITransport:
    """Make one fixed HTTPS request without redirect handling."""

    def __init__(
        self,
        connection_factory: Optional[Callable[..., Any]] = None,
        timeout_seconds: int = 120,
    ) -> None:
        self._connection_factory = connection_factory or http.client.HTTPSConnection
        self._timeout_seconds = timeout_seconds

    def __call__(self, body: bytes, api_key: str) -> HTTPUpstreamResponse:
        context = ssl.create_default_context()
        connection = self._connection_factory(
            UPSTREAM_HOST,
            UPSTREAM_PORT,
            timeout=self._timeout_seconds,
            context=context,
        )
        headers = {
            "Accept": "text/event-stream",
            "Authorization": "Bearer " + api_key,
            "Connection": "close",
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
        }
        try:
            connection.request("POST", UPSTREAM_PATH, body=body, headers=headers)
            response = connection.getresponse()
        except Exception:
            connection.close()
            raise
        return HTTPUpstreamResponse(response, connection)


@dataclass
class ProxyResponse:
    status: int
    headers: Tuple[Tuple[str, str], ...]
    chunks: Iterable[bytes]
    close_callback: Callable[[], None] = lambda: None

    def close(self) -> None:
        self.close_callback()


def _fixed_response(status: int, code: str) -> ProxyResponse:
    body = _json_error(code)
    return ProxyResponse(
        status=status,
        headers=(
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ),
        chunks=(body,),
    )


def _safe_upstream_headers(
    headers: Sequence[Tuple[str, str]], credentials: CredentialEnvelope
) -> Tuple[Tuple[str, str], ...]:
    content_types = [
        str(value)
        for name, value in headers
        if str(name).strip().lower() == "content-type"
    ]
    content_type = "application/octet-stream"
    if len(content_types) == 1:
        candidate = content_types[0]
        contains_secret = any(
            secret in candidate
            for secret in (
                credentials.api_key,
                credentials.sentinel,
                credentials.capability,
            )
        )
        if (
            "\r" not in candidate
            and "\n" not in candidate
            and len(candidate) <= 200
            and not contains_secret
        ):
            content_type = candidate
    return (("Content-Type", content_type),)


def _declared_response_length(headers: Sequence[Tuple[str, str]]) -> Optional[int]:
    values = [
        str(value).strip()
        for name, value in headers
        if str(name).strip().lower() == "content-length"
    ]
    if not values:
        return None
    if (
        len(values) != 1
        or len(values[0]) > 10
        or _CONTENT_LENGTH.fullmatch(values[0]) is None
    ):
        raise ResponseLimitError("invalid_upstream_content_length")
    return int(values[0])


def _bounded_chunks(
    upstream: Any, credentials: CredentialEnvelope
) -> Iterable[bytes]:
    secrets = tuple(secret for secret in credentials.secret_bytes if secret)
    retained = max(len(secret) for secret in secrets) - 1
    pending = b""
    received = 0
    while True:
        allowance = MAX_RESPONSE_BYTES - received
        chunk = upstream.read(min(IO_CHUNK_BYTES, allowance + 1))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise ResponseLimitError("invalid_upstream_body")
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise ResponseLimitError("upstream_body_too_large")
        pending += chunk
        if any(secret in pending for secret in secrets):
            raise ResponseLimitError("credential_material_in_upstream_body")
        safe_length = max(0, len(pending) - retained)
        if safe_length:
            yield pending[:safe_length]
            pending = pending[safe_length:]
    if any(secret in pending for secret in secrets):
        raise ResponseLimitError("credential_material_in_upstream_body")
    if pending:
        yield pending


class ResponsesProxy:
    """Policy and forwarding logic independent from the socket server."""

    def __init__(
        self,
        expected_model: str,
        credentials: CredentialEnvelope,
        transport: Optional[Callable[[bytes, str], Any]] = None,
        gate: Optional[RequestGate] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if not expected_model or not expected_model.strip():
            raise ValueError("expected model is required")
        self.expected_model = expected_model
        self.credentials = credentials
        self.transport = transport or OpenAITransport()
        self.gate = gate or RequestGate()
        self.logger = logger or logging.getLogger("diagram_grill.responses_proxy")

    def _log(self, event: str, **fields: Any) -> None:
        metadata = " ".join(
            "{}={}".format(key, fields[key]) for key in sorted(fields)
        )
        self.logger.info("event=%s%s", event, " " + metadata if metadata else "")

    def _reject(self, error: PolicyError) -> ProxyResponse:
        self._log("request_rejected", code=error.code, status=error.status)
        return _fixed_response(error.status, error.code)

    def handle(
        self,
        method: str,
        target: str,
        headers: Any,
        body_stream: Optional[BinaryIO] = None,
    ) -> ProxyResponse:
        normalized_headers = _header_items(headers)
        try:
            if any(name in FORBIDDEN_INBOUND_HEADERS for name, _ in normalized_headers):
                raise PolicyError(400, "inbound_credentials_rejected")
            if _header_values(normalized_headers, "transfer-encoding"):
                raise PolicyError(400, "transfer_encoding_rejected")
            if method not in ("GET", "POST"):
                raise PolicyError(405, "method_not_allowed")
            if _is_absolute_target(target):
                raise PolicyError(400, "absolute_target_rejected")

            if method == "GET" and target == "/healthz":
                if not self.gate.active():
                    raise PolicyError(503, "proxy_expired")
                body = b'{"ok":true}'
                return ProxyResponse(
                    200,
                    (
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ),
                    (body,),
                )
            if method == "GET":
                raise PolicyError(405, "method_not_allowed")
            if target != UPSTREAM_PATH:
                raise PolicyError(404, "path_not_found")

            capabilities = _header_values(normalized_headers, _CAPABILITY_HEADER)
            if (
                len(capabilities) != 1
                or not capabilities[0].isascii()
                or not hmac.compare_digest(
                    capabilities[0], self.credentials.capability
                )
            ):
                raise PolicyError(401, "client_capability_rejected")

            lengths = _header_values(normalized_headers, "content-length")
            if not lengths:
                raise PolicyError(411, "content_length_required")
            if (
                len(lengths) != 1
                or len(lengths[0]) > 10
                or _CONTENT_LENGTH.fullmatch(lengths[0]) is None
            ):
                raise PolicyError(400, "invalid_content_length")
            length = int(lengths[0])
            if length > MAX_REQUEST_BYTES:
                raise PolicyError(413, "request_body_too_large")
            if body_stream is None:
                raise PolicyError(400, "request_body_missing")
            raw_body = body_stream.read(length)
            if not isinstance(raw_body, bytes) or len(raw_body) != length:
                raise PolicyError(400, "incomplete_request_body")
            sanitized = _sanitize_payload(raw_body, self.expected_model, self.credentials)
            request_number = self.gate.reserve()
        except PolicyError as error:
            return self._reject(error)

        self._log("upstream_request", bytes=len(sanitized), request=request_number)
        try:
            upstream = self.transport(sanitized, self.credentials.api_key)
        except Exception:
            self._log("upstream_failure", request=request_number, status=502)
            return _fixed_response(502, "upstream_unavailable")

        status = getattr(upstream, "status", None)
        upstream_headers = list(getattr(upstream, "headers", ()))
        try:
            if not isinstance(status, int) or not 200 <= status <= 599:
                raise ResponseLimitError("invalid_upstream_status")
            if 300 <= status <= 399:
                raise ResponseLimitError("upstream_redirect_rejected")
            declared_length = _declared_response_length(upstream_headers)
            if declared_length is not None and declared_length > MAX_RESPONSE_BYTES:
                raise ResponseLimitError("upstream_body_too_large")
        except ResponseLimitError as error:
            upstream.close()
            self._log("upstream_rejected", code=str(error), request=request_number, status=502)
            return _fixed_response(502, "upstream_response_rejected")

        return ProxyResponse(
            status=status,
            headers=_safe_upstream_headers(upstream_headers, self.credentials),
            chunks=_bounded_chunks(upstream, self.credentials),
            close_callback=upstream.close,
        )


class ResponsesProxyHandler(BaseHTTPRequestHandler):
    """Thin HTTP adapter; all testable policy lives in ResponsesProxy."""

    protocol_version = "HTTP/1.1"
    server_version = "diagram-grill-eval-proxy"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_error(
        self,
        code: int,
        message: Optional[str] = None,
        explain: Optional[str] = None,
    ) -> None:
        """Replace BaseHTTPRequestHandler's request-reflecting HTML errors."""

        del message, explain
        body = _json_error("malformed_http_request")
        self.close_connection = True
        self.send_response_only(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def handle_expect_100(self) -> bool:
        response = _fixed_response(417, "expectation_rejected")
        self._write_response(response)
        return False

    def _write_response(self, response: ProxyResponse, send_body: bool = True) -> None:
        self.close_connection = True
        self.send_response(response.status)
        for name, value in response.headers:
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        sent = 0
        try:
            if send_body:
                for chunk in response.chunks:
                    self.wfile.write(chunk)
                    sent += len(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ResponseLimitError):
            proxy = getattr(self.server, "proxy", None)
            if proxy is not None:
                proxy._log("response_aborted", bytes=sent, status=response.status)
        finally:
            response.close()

    def _dispatch(self, send_body: bool = True) -> None:
        proxy = getattr(self.server, "proxy")
        response = proxy.handle(self.command, self.path, self.headers, self.rfile)
        self._write_response(response, send_body=send_body)

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch(send_body=False)

    def do_CONNECT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_TRACE(self) -> None:
        self._dispatch()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)


class ResponsesProxyServer(HTTPServer):
    def __init__(self, address: Tuple[str, int], proxy: ResponsesProxy) -> None:
        self.proxy = proxy
        super().__init__(address, ResponsesProxyHandler)


def serve_until_expired(server: ResponsesProxyServer) -> None:
    """Serve one request at a time and stop no later than the fixed TTL."""

    try:
        while server.proxy.gate.active():
            remaining = server.proxy.gate.remaining()
            server.timeout = max(0.05, min(0.5, remaining))
            server.handle_request()
    finally:
        server.server_close()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the fixed evaluation Responses proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--model", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None, stdin: Any = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if not 0 <= args.port <= 65535:
        print("invalid proxy port", file=sys.stderr)
        return 2
    try:
        if stdin is None:
            stdin = sys.stdin
        credentials = read_credential_envelope(stdin)
    except CredentialError:
        print("credential envelope rejected", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    proxy = ResponsesProxy(args.model, credentials)
    try:
        server = ResponsesProxyServer((args.host, args.port), proxy)
    except OSError:
        print("proxy listener could not start", file=sys.stderr)
        return 2
    proxy._log("proxy_started", port=server.server_address[1])
    serve_until_expired(server)
    proxy._log("proxy_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
