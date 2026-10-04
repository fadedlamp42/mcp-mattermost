"""load mattermost's openapi spec and correct the places where it lies.

the spec is built from mattermost's own sources at the server's version tag
(scripts/build-spec.sh). it is native openapi 3.0, so the blueprint's
swagger-2.0 patches (auth header stripping, server injection) mostly do not
apply; what does apply is listed in patch_spec, each with the evidence that
made it necessary.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"
HTTP_METHODS = ("get", "post", "put", "patch", "delete")

# two operations share the id SearchFiles in v11.5.1 (team-scoped and global).
# the team-scoped one keeps the name; the global one is renamed so every
# operation id is unique, which find/describe/call all key on.
OPERATION_ID_OVERRIDES: dict[tuple[str, str], str] = {
    ("post", "/api/v4/files/search"): "SearchFilesAcrossTeams",
}


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    summary: str
    description: str
    tags: tuple[str, ...]


def load_spec(base_url: str, spec_path: Path = SPEC_PATH) -> dict[str, Any]:
    spec = yaml.safe_load(spec_path.read_text())
    patch_spec(spec, base_url)
    return spec


def patch_spec(spec: dict[str, Any], base_url: str) -> None:
    # the spec's server is a template variable ({your-mattermost-url}); paths
    # already carry /api/v4, so the server is the bare origin.
    spec["servers"] = [{"url": base_url.rstrip("/")}]
    _fix_misindented_media_types(spec)
    _fill_missing_response_descriptions(spec)
    _assign_operation_ids(spec)


def _fix_misindented_media_types(node: Any) -> None:
    """users.yaml has `application/json: null` with its `schema` as a sibling
    (custom_profile_attributes GET 200). fastmcp refuses to parse the whole
    spec over it, so the schema is moved under the media type it belongs to."""
    if isinstance(node, dict):
        content = node.get("content")
        if isinstance(content, dict) and "schema" in content:
            null_media_types = [key for key, value in content.items() if value is None]
            if null_media_types:
                content[null_media_types[0]] = {"schema": content.pop("schema")}
        for value in list(node.values()):
            _fix_misindented_media_types(value)
    if isinstance(node, list):
        for item in node:
            _fix_misindented_media_types(item)


def _fill_missing_response_descriptions(spec: dict[str, Any]) -> None:
    for operation in _iterate_operations(spec):
        for response in (operation.get("responses") or {}).values():
            if isinstance(response, dict) and "$ref" not in response and "description" not in response:
                response["description"] = "OK"


def _assign_operation_ids(spec: dict[str, Any]) -> None:
    """every operation gets a unique, stable id.

    twelve content-flagging operations ship with no operationId; their id is
    derived from the summary ("Flag a post" -> FlagAPost), which survives a
    spec rebuild as long as the summary does. duplicates are resolved through
    OPERATION_ID_OVERRIDES, and any duplicate left over fails loudly here so a
    future spec cannot silently shadow one endpoint with another."""
    for path, path_item in spec.get("paths", {}).items():
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            override = OPERATION_ID_OVERRIDES.get((method, path))
            if override:
                operation["operationId"] = override
            if not operation.get("operationId"):
                operation["operationId"] = _identifier_from_summary(operation.get("summary") or f"{method} {path}")

    seen: dict[str, str] = {}
    for path, path_item in spec.get("paths", {}).items():
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            operation_id = operation["operationId"]
            location = f"{method.upper()} {path}"
            if operation_id in seen:
                raise ValueError(f"duplicate operationId {operation_id}: {seen[operation_id]} and {location}")
            seen[operation_id] = location


def _identifier_from_summary(summary: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", summary)
    return "".join(word[:1].upper() + word[1:] for word in words)


def _iterate_operations(spec: dict[str, Any]):
    for path_item in spec.get("paths", {}).values():
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if isinstance(operation, dict):
                yield operation


def index_operations(spec: dict[str, Any]) -> dict[str, Operation]:
    """operationId -> Operation, for find/describe/call."""
    operations: dict[str, Operation] = {}
    for path, path_item in spec.get("paths", {}).items():
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            operations[operation["operationId"]] = Operation(
                operation_id=operation["operationId"],
                method=method.upper(),
                path=path,
                summary=(operation.get("summary") or "").strip(),
                description=(operation.get("description") or "").strip(),
                tags=tuple(operation.get("tags") or ()),
            )
    return operations
