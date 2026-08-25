"""Discover and search the pinned :mod:`diagrams` node catalog safely.

The catalog is built only from the installed ``diagrams==0.25.1``
distribution.  Repository files are never imported, and node resolution is
limited to exact import paths that were observed during discovery.
"""

from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
import importlib.util
import inspect
import os
import pkgutil
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Tuple, Type


EXPECTED_DIAGRAMS_VERSION = "0.25.1"
GENERIC_FALLBACK_IMPORT_PATH = "diagrams.generic.blank.Blank"
DEFAULT_SEARCH_LIMIT = 20

_INTERNAL_TOP_LEVEL_MODULES = frozenset({"base", "cli", "custom"})
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_ALPHANUMERIC_TOKEN = re.compile(r"[A-Za-z0-9]+")


class NodeCatalogError(RuntimeError):
    """Base error for catalog discovery and resolution failures."""


class DiagramsVersionError(NodeCatalogError):
    """Raised when the installed diagrams version is not the pinned one."""


class UnknownNodeError(NodeCatalogError):
    """Raised when an import path is not present in the discovered catalog."""


@dataclass(frozen=True)
class CatalogEntry:
    """One exact, importable public ``diagrams`` node symbol."""

    import_path: str
    provider: str
    category: str
    class_name: str
    search_text: str


def _split_tokens(value: str) -> Tuple[str, ...]:
    expanded = _ACRONYM_BOUNDARY.sub(" ", value)
    expanded = _CAMEL_BOUNDARY.sub(" ", expanded)
    return tuple(token.casefold() for token in _ALPHANUMERIC_TOKEN.findall(expanded))


def _build_search_text(
    import_path: str,
    provider: str,
    category: str,
    class_name: str,
    target_class_name: str,
) -> str:
    # Keep both compact identifiers (``ec2instance``) and their split tokens
    # (``ec2 instance``).  The target class name makes documented aliases such
    # as ``AMI`` searchable by the class they reference without changing the
    # exact import path returned to callers.
    values = (import_path, provider, category, class_name, target_class_name)
    parts = []
    seen = set()
    for value in values:
        compact = value.casefold()
        for part in (compact,) + _split_tokens(value):
            if part and part not in seen:
                seen.add(part)
                parts.append(part)
    return " ".join(parts)


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _validated_package_dir() -> Path:
    try:
        installed_version = importlib_metadata.version("diagrams")
    except importlib_metadata.PackageNotFoundError as exc:
        raise NodeCatalogError("diagrams is not installed") from exc

    if installed_version != EXPECTED_DIAGRAMS_VERSION:
        raise DiagramsVersionError(
            "diagram-grill requires diagrams=={expected}; found {actual}".format(
                expected=EXPECTED_DIAGRAMS_VERSION,
                actual=installed_version,
            )
        )

    distribution = importlib_metadata.distribution("diagrams")
    package_dir = Path(distribution.locate_file("diagrams")).resolve()
    package_spec = importlib.util.find_spec("diagrams")
    if package_spec is None or package_spec.origin is None:
        raise NodeCatalogError("could not locate the installed diagrams package")

    imported_package_dir = Path(package_spec.origin).resolve().parent
    if imported_package_dir != package_dir:
        raise NodeCatalogError(
            "refusing to import diagrams outside its installed distribution: "
            + str(imported_package_dir)
        )
    return package_dir


def _validate_module_origin(module_name: str, package_dir: Path) -> None:
    module_spec = importlib.util.find_spec(module_name)
    if module_spec is None or module_spec.origin is None:
        raise NodeCatalogError("could not locate diagrams module: " + module_name)

    origin = Path(module_spec.origin).resolve()
    if not _is_within(origin, package_dir):
        raise NodeCatalogError(
            "refusing to import a diagrams module outside the pinned package: "
            + module_name
        )


def _discover_node_catalog_uncached() -> Tuple[CatalogEntry, ...]:
    package_dir = _validated_package_dir()
    diagrams_package = importlib.import_module("diagrams")
    node_type = getattr(diagrams_package, "Node", None)
    if not inspect.isclass(node_type):
        raise NodeCatalogError("the installed diagrams package has no Node base class")

    entries = {}
    module_infos = sorted(
        pkgutil.walk_packages(
            diagrams_package.__path__, diagrams_package.__name__ + "."
        ),
        key=lambda module_info: module_info.name,
    )

    for module_info in module_infos:
        module_name = module_info.name
        name_parts = module_name.split(".")
        # The IR accepts only diagrams.<provider>.<category>.<Class>.  Public
        # provider-root nodes such as diagrams.aws.AWS are real Node classes,
        # but their empty category makes them invalid IR icon paths.
        if len(name_parts) != 3:
            continue

        provider = name_parts[1]
        if provider in _INTERNAL_TOP_LEVEL_MODULES:
            continue

        _validate_module_origin(module_name, package_dir)
        module = importlib.import_module(module_name)
        category = ".".join(name_parts[2:])

        # Reading vars(module) avoids invoking dynamic module attributes.  A
        # class must be public, owned by this exact module, and inherit Node.
        # Module-owned aliases remain valid because their symbol is genuinely
        # importable from the reported path.
        for class_name in sorted(vars(module)):
            if class_name.startswith("_") or not class_name.isidentifier():
                continue

            candidate = vars(module)[class_name]
            if not inspect.isclass(candidate) or candidate is node_type:
                continue
            try:
                is_node = issubclass(candidate, node_type)
            except TypeError:
                is_node = False
            if not is_node or getattr(candidate, "__module__", None) != module_name:
                continue

            import_path = module_name + "." + class_name
            entry = CatalogEntry(
                import_path=import_path,
                provider=provider,
                category=category,
                class_name=class_name,
                search_text=_build_search_text(
                    import_path,
                    provider,
                    category,
                    class_name,
                    getattr(candidate, "__name__", class_name),
                ),
            )
            if import_path in entries:
                raise NodeCatalogError("duplicate diagrams node path: " + import_path)
            entries[import_path] = entry

    if not entries:
        raise NodeCatalogError("no public diagrams Node subclasses were discovered")

    return tuple(entries[path] for path in sorted(entries))


@lru_cache(maxsize=1)
def discover_node_catalog() -> Tuple[CatalogEntry, ...]:
    """Return all public node symbols from the pinned installed distribution.

    Results are sorted by exact import path and cached for the life of the
    process.  Version and package-origin checks happen before provider modules
    are imported.
    """

    return _discover_node_catalog_uncached()


def _normalize_filter(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(name + " must be a string or None")
    normalized = value.strip().casefold()
    if not normalized and name == "provider":
        raise ValueError("provider must not be empty")
    return normalized


def search_node_catalog(
    query: str = "",
    *,
    provider: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> Tuple[CatalogEntry, ...]:
    """Search provider, category, and class tokens deterministically.

    ``provider`` and ``category`` are optional case-insensitive exact filters.
    Query tokens must all occur in an entry's normalized search text.  Exact
    class matches rank first, followed by class prefixes and then import-path
    order.  At most ``limit`` entries are returned.
    """

    if not isinstance(query, str):
        raise TypeError("query must be a string")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be at least 1")

    provider_filter = _normalize_filter(provider, "provider")
    category_filter = _normalize_filter(category, "category")
    stripped_query = query.strip()
    query_tokens = _split_tokens(stripped_query)
    if stripped_query and not query_tokens:
        raise ValueError("query must contain at least one alphanumeric token")

    matches = []
    for entry in discover_node_catalog():
        if provider_filter is not None and entry.provider.casefold() != provider_filter:
            continue
        if category_filter is not None and entry.category.casefold() != category_filter:
            continue
        if query_tokens and not all(token in entry.search_text for token in query_tokens):
            continue
        matches.append(entry)

    query_folded = stripped_query.casefold()
    query_token_set = set(query_tokens)

    def sort_key(entry: CatalogEntry) -> Tuple[int, str]:
        class_name = entry.class_name.casefold()
        if query_folded and (
            class_name == query_folded or class_name in query_token_set
        ):
            rank = 0
        elif query_folded and (
            class_name.startswith(query_folded)
            or any(class_name.startswith(token) for token in query_tokens)
        ):
            rank = 1
        else:
            rank = 2
        return rank, entry.import_path

    matches.sort(key=sort_key)
    return tuple(matches[:limit])


def resolve_node(import_path: str) -> Type[Any]:
    """Resolve one exact catalog path to its installed ``Node`` subclass.

    No fuzzy matching, provider guessing, or fallback occurs here.  Call
    :func:`generic_fallback` explicitly when a requested provider node does
    not exist.
    """

    if not isinstance(import_path, str):
        raise TypeError("import_path must be a string")

    entry = next(
        (item for item in discover_node_catalog() if item.import_path == import_path),
        None,
    )
    if entry is None:
        raise UnknownNodeError("unknown diagrams node import path: " + import_path)

    module_name, _, class_name = import_path.rpartition(".")
    module = importlib.import_module(module_name)
    candidate = getattr(module, class_name, None)
    diagrams_package = importlib.import_module("diagrams")
    node_type = getattr(diagrams_package, "Node")
    if (
        not inspect.isclass(candidate)
        or candidate is node_type
        or not issubclass(candidate, node_type)
        or getattr(candidate, "__module__", None) != module_name
    ):
        raise NodeCatalogError("catalog path no longer resolves to a Node: " + import_path)
    return candidate


def generic_fallback() -> CatalogEntry:
    """Return the one documented neutral fallback, ``generic.blank.Blank``.

    The helper fails explicitly if the pinned class or its bundled local icon
    is unavailable.  It never substitutes a provider-specific icon.
    """

    entry = next(
        (
            item
            for item in discover_node_catalog()
            if item.import_path == GENERIC_FALLBACK_IMPORT_PATH
        ),
        None,
    )
    if entry is None:
        raise NodeCatalogError(
            "required Generic fallback is unavailable: "
            + GENERIC_FALLBACK_IMPORT_PATH
        )
    if (
        entry.provider != "generic"
        or entry.category != "blank"
        or entry.class_name != "Blank"
    ):
        raise NodeCatalogError("Generic fallback catalog metadata is invalid")

    fallback_type = resolve_node(entry.import_path)
    icon_dir = getattr(fallback_type, "_icon_dir", None)
    icon_name = getattr(fallback_type, "_icon", None)
    if not isinstance(icon_dir, str) or not isinstance(icon_name, str):
        raise NodeCatalogError("Generic fallback does not declare a bundled icon")

    package_dir = Path(importlib.import_module("diagrams").__file__).resolve().parent
    icon_path = (package_dir.parent / icon_dir / icon_name).resolve()
    if not _is_within(icon_path, package_dir.parent) or not icon_path.is_file():
        raise NodeCatalogError("Generic fallback icon is unavailable: " + str(icon_path))
    return entry


__all__ = [
    "CatalogEntry",
    "DEFAULT_SEARCH_LIMIT",
    "DiagramsVersionError",
    "EXPECTED_DIAGRAMS_VERSION",
    "GENERIC_FALLBACK_IMPORT_PATH",
    "NodeCatalogError",
    "UnknownNodeError",
    "discover_node_catalog",
    "generic_fallback",
    "resolve_node",
    "search_node_catalog",
]
