import io
import json
import logging
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "evals"))

import responses_proxy  # noqa: E402


API_KEY = "sk-test-credential-that-must-never-appear"
SENTINEL = "sentinel-0123456789abcdef"
CAPABILITY = "0123456789abcdef" * 4
MODEL = "test-model"


def valid_payload(**changes):
    payload = {
        "model": MODEL,
        "stream": True,
        "reasoning": {"effort": "medium", "summary": "auto"},
        "service_tier": "default",
        "tools": [
            {
                "type": "function",
                "name": "read_file",
                "description": "Read one workspace file.",
                "strict": False,
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "type": "custom",
                "name": "shell",
                "description": "Run one sandboxed shell command.",
                "format": {
                    "type": "grammar",
                    "syntax": "lark",
                    "definition": "start: /.+/",
                },
            },
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "instructions": "Use only the supplied local tools.",
        "prompt_cache_key": "eval-session",
        "client_metadata": {"session_id": "session-test"},
        "text": {"verbosity": "medium"},
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "draw the architecture"}
                ],
            }
        ],
    }
    payload.update(changes)
    return payload


def request_body(payload):
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class FakeUpstream:
    def __init__(self, body=b"event: done\ndata: {}\n\n", status=200, headers=None):
        self.status = status
        self.reason = "test"
        self.headers = headers or [("Content-Type", "text/event-stream")]
        self._body = io.BytesIO(body)
        self.closed = False

    def read(self, amount):
        return self._body.read(amount)

    def close(self):
        self.closed = True


class RecordingTransport:
    def __init__(self, response=None, error=None):
        self.response = response or FakeUpstream()
        self.error = error
        self.calls = []

    def __call__(self, body, api_key):
        self.calls.append((body, api_key))
        if self.error is not None:
            raise self.error
        return self.response


def make_proxy(transport=None, clock=None, max_requests=24, ttl_seconds=1200):
    credentials = responses_proxy.CredentialEnvelope(API_KEY, SENTINEL, CAPABILITY)
    if clock is None:
        clock = lambda: 10.0
    gate = responses_proxy.RequestGate(
        clock=clock, max_requests=max_requests, ttl_seconds=ttl_seconds
    )
    return responses_proxy.ResponsesProxy(
        MODEL,
        credentials,
        transport=transport or RecordingTransport(),
        gate=gate,
    )


def dispatch(
    proxy,
    method="POST",
    target="/v1/responses",
    payload=None,
    headers=None,
    capability=CAPABILITY,
):
    body = request_body(valid_payload() if payload is None else payload)
    selected_headers = [("Content-Length", str(len(body)))]
    if headers is not None:
        selected_headers = list(headers)
    if capability is not None:
        selected_headers.append((responses_proxy.CAPABILITY_HEADER, capability))
    return proxy.handle(method, target, selected_headers, io.BytesIO(body))


def response_bytes(response):
    try:
        return b"".join(response.chunks)
    finally:
        response.close()


class CredentialEnvelopeTests(unittest.TestCase):
    def test_reads_one_strict_envelope_and_redacts_repr(self):
        envelope = responses_proxy.read_credential_envelope(
            io.BytesIO(
                request_body(
                    {
                        "api_key": API_KEY,
                        "sentinel": SENTINEL,
                        "capability": CAPABILITY,
                    }
                )
            )
        )

        self.assertEqual(API_KEY, envelope.api_key)
        self.assertEqual(SENTINEL, envelope.sentinel)
        self.assertEqual(CAPABILITY, envelope.capability)
        self.assertNotIn(API_KEY, repr(envelope))
        self.assertNotIn(SENTINEL, repr(envelope))
        self.assertNotIn(CAPABILITY, repr(envelope))

    def test_rejects_extra_fields_trailing_json_and_short_sentinel_without_reflection(self):
        cases = [
            {
                "api_key": API_KEY,
                "sentinel": SENTINEL,
                "capability": CAPABILITY,
                "extra": True,
            },
            {"api_key": API_KEY, "sentinel": "short", "capability": CAPABILITY},
            {"api_key": API_KEY, "sentinel": SENTINEL, "capability": "short"},
        ]
        for value in cases:
            with self.subTest(value=set(value)):
                with self.assertRaises(responses_proxy.CredentialError) as raised:
                    responses_proxy.read_credential_envelope(
                        io.StringIO(json.dumps(value))
                    )
                self.assertNotIn(API_KEY, str(raised.exception))
                self.assertNotIn(SENTINEL, str(raised.exception))
                self.assertNotIn(CAPABILITY, str(raised.exception))
        with self.assertRaises(responses_proxy.CredentialError):
            responses_proxy.read_credential_envelope(
                io.StringIO(json.dumps(cases[0]) + json.dumps(cases[0]))
            )

    def test_main_credential_error_does_not_echo_input(self):
        stderr = io.StringIO()
        bad_envelope = json.dumps(
            {"api_key": API_KEY, "sentinel": "short", "capability": CAPABILITY}
        )
        with redirect_stderr(stderr):
            status = responses_proxy.main(
                ["--model", MODEL], stdin=io.StringIO(bad_envelope)
            )

        self.assertEqual(2, status)
        self.assertNotIn(API_KEY, stderr.getvalue())
        self.assertNotIn("short", stderr.getvalue())
        self.assertNotIn(CAPABILITY, stderr.getvalue())


class RequestPolicyTests(unittest.TestCase):
    def test_health_is_fixed_and_does_not_call_upstream(self):
        transport = RecordingTransport()
        proxy = make_proxy(transport)

        response = proxy.handle("GET", "/healthz", [], io.BytesIO())

        self.assertEqual(200, response.status)
        self.assertEqual(b'{"ok":true}', response_bytes(response))
        self.assertEqual([], transport.calls)

    def test_capability_is_required_before_budget_or_upstream(self):
        transport = RecordingTransport()
        proxy = make_proxy(transport, max_requests=1)

        rejected = [
            dispatch(proxy, capability=None),
            dispatch(proxy, capability="f" * 64),
            dispatch(proxy, capability="é" * 64),
            dispatch(
                proxy,
                headers=[
                    ("Content-Length", str(len(request_body(valid_payload())))),
                    (responses_proxy.CAPABILITY_HEADER, CAPABILITY),
                    (responses_proxy.CAPABILITY_HEADER, CAPABILITY),
                ],
                capability=None,
            ),
        ]
        for response in rejected:
            self.assertEqual(401, response.status)
            self.assertIn(b"client_capability_rejected", response_bytes(response))
        self.assertEqual([], transport.calls)

        accepted = dispatch(proxy)
        self.assertEqual(200, accepted.status)
        accepted.close()
        self.assertEqual(1, len(transport.calls))
        self.assertEqual(429, dispatch(proxy).status)

    def test_only_exact_method_and_path_are_allowed(self):
        proxy = make_proxy()
        cases = [
            ("CONNECT", "api.openai.com:443", 405),
            ("PUT", "/v1/responses", 405),
            ("POST", "/v1/responses?x=1", 404),
            ("POST", "https://api.openai.com/v1/responses", 400),
            ("GET", "//api.openai.com/healthz", 400),
        ]
        for method, target, expected_status in cases:
            with self.subTest(method=method, target=target):
                self.assertEqual(expected_status, dispatch(proxy, method, target).status)

    def test_rejects_inbound_auth_and_cookie_headers(self):
        for header in ("Authorization", "Cookie", "Proxy-Authorization"):
            with self.subTest(header=header):
                response = dispatch(
                    make_proxy(),
                    headers=[("Content-Length", "2"), (header, "secret")],
                )
                self.assertEqual(400, response.status)
                self.assertIn(b"inbound_credentials_rejected", response_bytes(response))

    def test_benign_inbound_headers_are_not_forwarded(self):
        transport = RecordingTransport()
        proxy = make_proxy(transport)
        body = request_body(valid_payload())
        response = proxy.handle(
            "POST",
            "/v1/responses",
            [
                ("Content-Length", str(len(body))),
                (responses_proxy.CAPABILITY_HEADER, CAPABILITY),
                ("User-Agent", "untrusted-client"),
                ("X-Api-Key", "untrusted-key"),
                ("OpenAI-Organization", "untrusted-org"),
            ],
            io.BytesIO(body),
        )

        self.assertEqual(200, response.status)
        response.close()
        self.assertEqual(1, len(transport.calls))
        self.assertEqual(API_KEY, transport.calls[0][1])
        self.assertNotIn(b"untrusted", transport.calls[0][0])
        self.assertNotIn(CAPABILITY.encode(), transport.calls[0][0])

    def test_content_length_is_required_single_decimal_bounded_and_complete(self):
        proxy = make_proxy()
        body = request_body(valid_payload())
        cases = [
            ([], io.BytesIO(body), 411),
            ([("Content-Length", "+1")], io.BytesIO(body), 400),
            ([("Content-Length", "9" * 5000)], io.BytesIO(body), 400),
            (
                [("Content-Length", str(len(body))), ("Content-Length", str(len(body)))],
                io.BytesIO(body),
                400,
            ),
            (
                [("Content-Length", str(responses_proxy.MAX_REQUEST_BYTES + 1))],
                io.BytesIO(body),
                413,
            ),
            ([("Content-Length", str(len(body) + 1))], io.BytesIO(body), 400),
            (
                [("Content-Length", str(len(body))), ("Transfer-Encoding", "chunked")],
                io.BytesIO(body),
                400,
            ),
        ]
        for headers, stream, status in cases:
            with self.subTest(headers=headers):
                authorized = list(headers) + [
                    (responses_proxy.CAPABILITY_HEADER, CAPABILITY)
                ]
                response = proxy.handle("POST", "/v1/responses", authorized, stream)
                self.assertEqual(status, response.status)

    def test_requires_json_object_model_stream_reasoning_and_service_tier(self):
        cases = [
            ([], "json_object_required"),
            ({}, "model_mismatch"),
            (valid_payload(model="other"), "model_mismatch"),
            (valid_payload(stream=False), "stream_required"),
            (valid_payload(reasoning={"effort": "high"}), "medium_reasoning_required"),
            (valid_payload(reasoning=None), "medium_reasoning_required"),
            (valid_payload(service_tier="auto"), "default_service_tier_required"),
            (valid_payload(service_tier=None), "default_service_tier_required"),
        ]
        for payload, code in cases:
            with self.subTest(code=code):
                response = dispatch(make_proxy(), payload=payload)
                self.assertEqual(400, response.status)
                self.assertIn(code.encode("ascii"), response_bytes(response))

        nonfinite = (
            b'{"model":"test-model","stream":true,"reasoning":{"effort":"medium"},'
            b'"service_tier":"default","temperature":NaN}'
        )
        response = make_proxy().handle(
            "POST",
            "/v1/responses",
            [
                ("Content-Length", str(len(nonfinite))),
                (responses_proxy.CAPABILITY_HEADER, CAPABILITY),
            ],
            io.BytesIO(nonfinite),
        )
        self.assertEqual(400, response.status)
        self.assertIn(b"invalid_json", response_bytes(response))

    def test_allows_only_function_and_custom_tools(self):
        for tool_type in (
            "code_interpreter",
            "computer",
            "file_search",
            "image_generation",
            "mcp",
            "namespace",
            "tool_search",
            "unknown",
            "web_search",
        ):
            with self.subTest(tool_type=tool_type):
                response = dispatch(
                    make_proxy(), payload=valid_payload(tools=[{"type": tool_type}])
                )
                self.assertEqual(400, response.status)
                self.assertIn(b"tool_type_rejected", response_bytes(response))

        self.assertEqual(200, dispatch(make_proxy(), payload=valid_payload(tools=[])).status)
        self.assertEqual(200, dispatch(make_proxy(), payload=valid_payload()).status)

    def test_rejects_unknown_top_level_and_typed_nested_fields_without_upstream(self):
        cases = []

        top_level = valid_payload(unexpected=True)
        cases.append(top_level)

        reasoning = valid_payload()
        reasoning["reasoning"]["unexpected"] = True
        cases.append(reasoning)

        function_tool = valid_payload()
        function_tool["tools"][0]["unexpected"] = True
        cases.append(function_tool)

        custom_format = valid_payload()
        custom_format["tools"][1]["format"]["unexpected"] = True
        cases.append(custom_format)

        message = valid_payload()
        message["input"][0]["unexpected"] = True
        cases.append(message)

        for payload in cases:
            with self.subTest(keys=sorted(payload)):
                transport = RecordingTransport()
                response = dispatch(make_proxy(transport), payload=payload)
                self.assertEqual(400, response.status)
                self.assertIn(b"unknown_request_field", response_bytes(response))
                self.assertEqual([], transport.calls)

    def test_rejects_external_file_and_project_resources_recursively(self):
        cases = []

        image = valid_payload()
        image["input"][0]["content"] = [
            {"type": "input_image", "image_url": "https://example.test/image.png"}
        ]
        cases.append(image)

        file_reference = valid_payload()
        file_reference["input"][0]["content"] = [
            {"type": "input_file", "file_id": "file_attacker"}
        ]
        cases.append(file_reference)

        indirect_url = valid_payload()
        indirect_url["input"][0]["source"] = "https://example.test/resource"
        cases.append(indirect_url)

        project_resource = valid_payload()
        project_resource["input"][0]["project_resource"] = {
            "id": "resource_attacker"
        }
        cases.append(project_resource)

        output_image = valid_payload()
        output_image["input"].append(
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [
                    {"type": "input_image", "image_url": "data:image/png;base64,AA=="}
                ],
            }
        )
        cases.append(output_image)

        for payload in cases:
            transport = RecordingTransport()
            response = dispatch(make_proxy(transport), payload=payload)
            self.assertEqual(400, response.status)
            self.assertIn(b"resource_reference_rejected", response_bytes(response))
            self.assertEqual([], transport.calls)

    def test_valid_pinned_shape_allows_urls_only_as_opaque_text(self):
        transport = RecordingTransport()
        payload = valid_payload()
        payload["input"][0]["content"][0]["text"] = (
            "The checked-in documentation mentions https://example.test/reference."
        )
        payload["input"].append(
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "file content: https://example.test/not-fetched",
            }
        )

        response = dispatch(make_proxy(transport), payload=payload)

        self.assertEqual(200, response.status)
        response.close()
        self.assertEqual(1, len(transport.calls))
        forwarded = json.loads(transport.calls[0][0])
        self.assertEqual(payload["input"], forwarded["input"])

    def test_valid_pinned_shape_accepts_codex_multiturn_items(self):
        transport = RecordingTransport()
        payload = valid_payload()
        payload["input"].extend(
            [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [
                        {"type": "summary_text", "text": "Inspect the workspace."}
                    ],
                    "content": None,
                    "encrypted_content": "opaque-reasoning",
                },
                {
                    "type": "custom_tool_call",
                    "id": "ctc_1",
                    "status": "completed",
                    "call_id": "call_1",
                    "name": "shell",
                    "input": "pwd",
                },
                {
                    "type": "custom_tool_call_output",
                    "id": "ctco_1",
                    "call_id": "call_1",
                    "output": [
                        {"type": "input_text", "text": "/workspace\n"}
                    ],
                },
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [
                        {"type": "output_text", "text": "I inspected the workspace."}
                    ],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "turn-1",
                        "create_time": 1.25,
                    },
                },
            ]
        )

        response = dispatch(make_proxy(transport), payload=payload)

        self.assertEqual(200, response.status)
        response.close()
        self.assertEqual(payload["input"], json.loads(transport.calls[0][0])["input"])

    def test_forces_store_false_and_caps_output_tokens(self):
        cases = [(None, 16_000), (50_000, 16_000), (1234, 1234)]
        for requested, expected in cases:
            with self.subTest(requested=requested):
                transport = RecordingTransport()
                payload = valid_payload(store=True)
                if requested is not None:
                    payload["max_output_tokens"] = requested
                response = dispatch(make_proxy(transport), payload=payload)
                response_bytes(response)
                forwarded = json.loads(transport.calls[0][0])
                self.assertIs(False, forwarded["store"])
                self.assertEqual(expected, forwarded["max_output_tokens"])

    def test_rejects_invalid_output_token_values(self):
        for value in (True, 0, -1, "16000"):
            with self.subTest(value=value):
                response = dispatch(
                    make_proxy(), payload=valid_payload(max_output_tokens=value)
                )
                self.assertEqual(400, response.status)

    def test_rejects_credential_material_in_request_without_forwarding(self):
        for secret in (API_KEY, SENTINEL, CAPABILITY):
            with self.subTest(secret="redacted"):
                transport = RecordingTransport()
                payload = valid_payload()
                payload["input"][0]["content"][0]["text"] = "prefix " + secret
                response = dispatch(
                    make_proxy(transport), payload=payload
                )
                self.assertEqual(400, response.status)
                self.assertEqual([], transport.calls)
                body = response_bytes(response)
                self.assertNotIn(API_KEY.encode(), body)
                self.assertNotIn(SENTINEL.encode(), body)
                self.assertNotIn(CAPABILITY.encode(), body)


class BudgetAndUpstreamTests(unittest.TestCase):
    def test_request_budget_is_24_and_expiry_applies_to_health_and_post(self):
        proxy = make_proxy()
        for _ in range(24):
            response = dispatch(proxy)
            self.assertEqual(200, response.status)
            response.close()
        self.assertEqual(429, dispatch(proxy).status)

        now = [100.0]
        expired = make_proxy(clock=lambda: now[0], ttl_seconds=1200)
        now[0] = 1300.0
        self.assertEqual(503, expired.handle("GET", "/healthz", []).status)
        self.assertEqual(503, dispatch(expired).status)

    def test_transport_failure_redirect_and_large_declared_response_are_fixed_errors(self):
        cases = [
            (RecordingTransport(error=RuntimeError(API_KEY)), "upstream_unavailable"),
            (
                RecordingTransport(
                    FakeUpstream(status=302, headers=[("Location", SENTINEL)])
                ),
                "upstream_response_rejected",
            ),
            (
                RecordingTransport(
                    FakeUpstream(
                        headers=[
                            ("Content-Length", str(responses_proxy.MAX_RESPONSE_BYTES + 1))
                        ]
                    )
                ),
                "upstream_response_rejected",
            ),
        ]
        for transport, code in cases:
            with self.subTest(code=code):
                response = dispatch(make_proxy(transport))
                self.assertEqual(502, response.status)
                body = response_bytes(response)
                self.assertIn(code.encode(), body)
                self.assertNotIn(API_KEY.encode(), body)
                self.assertNotIn(SENTINEL.encode(), body)
                self.assertNotIn(CAPABILITY.encode(), body)

    def test_secret_upstream_content_type_is_not_reflected(self):
        for secret in (API_KEY, SENTINEL, CAPABILITY):
            with self.subTest(secret="redacted"):
                upstream = FakeUpstream(headers=[("Content-Type", "text/" + secret)])
                response = dispatch(make_proxy(RecordingTransport(upstream)))
                self.assertEqual(
                    (("Content-Type", "application/octet-stream"),), response.headers
                )
                response.close()

    def test_stream_is_capped_and_secret_values_are_not_emitted(self):
        too_large = FakeUpstream(body=b"x" * (responses_proxy.MAX_RESPONSE_BYTES + 1))
        response = dispatch(make_proxy(RecordingTransport(too_large)))
        emitted = bytearray()
        with self.assertRaises(responses_proxy.ResponseLimitError):
            for chunk in response.chunks:
                emitted.extend(chunk)
        response.close()
        self.assertLessEqual(len(emitted), responses_proxy.MAX_RESPONSE_BYTES)

        for secret in (API_KEY, SENTINEL, CAPABILITY):
            with self.subTest(secret="redacted"):
                upstream = FakeUpstream(body=b"safe-prefix" + secret.encode() + b"tail")
                response = dispatch(make_proxy(RecordingTransport(upstream)))
                emitted = bytearray()
                with self.assertRaises(responses_proxy.ResponseLimitError):
                    for chunk in response.chunks:
                        emitted.extend(chunk)
                response.close()
                self.assertNotIn(secret.encode(), emitted)

        class SplitSecretUpstream(FakeUpstream):
            def __init__(self):
                super().__init__(body=b"")
                self.parts = [b"safe-" + SENTINEL[:8].encode(), SENTINEL[8:].encode()]

            def read(self, amount):
                del amount
                return self.parts.pop(0) if self.parts else b""

        response = dispatch(make_proxy(RecordingTransport(SplitSecretUpstream())))
        emitted = bytearray()
        with self.assertRaises(responses_proxy.ResponseLimitError):
            for chunk in response.chunks:
                emitted.extend(chunk)
        response.close()
        self.assertNotIn(SENTINEL.encode(), emitted)


class TransportTests(unittest.TestCase):
    def test_fixed_https_destination_headers_and_no_inbound_header_forwarding(self):
        class FakeHTTPResponse:
            status = 200
            reason = "OK"

            def getheaders(self):
                return [("Content-Type", "text/event-stream")]

            def read(self, amount):
                return b""

            def close(self):
                pass

        class FakeConnection:
            def __init__(self, host, port, **kwargs):
                self.init = (host, port, kwargs)
                self.request_call = None
                self.closed = False

            def request(self, method, path, body, headers):
                self.request_call = (method, path, body, headers)

            def getresponse(self):
                return FakeHTTPResponse()

            def close(self):
                self.closed = True

        connections = []

        def factory(*args, **kwargs):
            connection = FakeConnection(*args, **kwargs)
            connections.append(connection)
            return connection

        transport = responses_proxy.OpenAITransport(connection_factory=factory)
        body = b'{"safe":true}'
        response = transport(body, API_KEY)
        connection = connections[0]

        self.assertEqual(
            (responses_proxy.UPSTREAM_HOST, responses_proxy.UPSTREAM_PORT),
            connection.init[:2],
        )
        method, path, forwarded_body, headers = connection.request_call
        self.assertEqual("POST", method)
        self.assertEqual("/v1/responses", path)
        self.assertEqual(body, forwarded_body)
        self.assertEqual("Bearer " + API_KEY, headers["Authorization"])
        self.assertEqual("close", headers["Connection"])
        self.assertEqual(str(len(body)), headers["Content-Length"])
        self.assertEqual(
            {"Accept", "Authorization", "Connection", "Content-Length", "Content-Type"},
            set(headers),
        )
        response.close()
        self.assertTrue(connection.closed)


class SecretLoggingTests(unittest.TestCase):
    def test_logs_never_include_request_response_or_credentials(self):
        output = io.StringIO()
        logger = logging.getLogger("responses-proxy-test")
        logger.handlers[:] = []
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler(output))
        transport = RecordingTransport(error=RuntimeError(API_KEY + SENTINEL))
        proxy = make_proxy(transport)
        proxy.logger = logger

        payload = valid_payload()
        payload["input"][0]["content"][0]["text"] = "private request text"
        response = dispatch(proxy, payload=payload)
        response_bytes(response)
        logs = output.getvalue()

        self.assertNotIn(API_KEY, logs)
        self.assertNotIn(SENTINEL, logs)
        self.assertNotIn(CAPABILITY, logs)
        self.assertNotIn("private request text", logs)
        self.assertIn("event=upstream_failure", logs)

        output.seek(0)
        output.truncate(0)
        successful = RecordingTransport(FakeUpstream(body=b"private response text"))
        proxy.transport = successful
        response_bytes(dispatch(proxy))
        self.assertNotIn("private response text", output.getvalue())

    def test_default_http_errors_do_not_reflect_the_request_line(self):
        handler = object.__new__(responses_proxy.ResponsesProxyHandler)
        handler.command = "GET"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "GET /" + SENTINEL + " HTTP/1.1"
        handler.wfile = io.BytesIO()
        handler.close_connection = False

        handler.send_error(400, API_KEY, SENTINEL)
        rendered = handler.wfile.getvalue()

        self.assertNotIn(API_KEY.encode(), rendered)
        self.assertNotIn(SENTINEL.encode(), rendered)
        self.assertIn(b"malformed_http_request", rendered)


if __name__ == "__main__":
    unittest.main()
