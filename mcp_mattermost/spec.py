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
# the mattermost tag the vendored spec was built from (scripts/build-spec.sh
# writes it); tool descriptions quote it so an agent knows which server
# version "every operation" means
SPEC_VERSION = (SPEC_PATH.parent / "openapi.version").read_text().strip()
HTTP_METHODS = ("get", "post", "put", "patch", "delete")
PATH_TEMPLATE_PARAMETER = re.compile(r"\{([^}]+)\}")

# two operations share the id SearchFiles since v11.5.1 (team-scoped and global).
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
    _declare_missing_path_parameters(spec)
    _accept_create_at_on_scheduled_post_update(spec)
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


def _declare_missing_path_parameters(spec: dict[str, Any]) -> None:
    """every {name} in a path gets a declared, required path parameter.

    GetUserScheduledPosts (/api/v4/posts/scheduled/team/{team_id}) declares
    only a query parameter, so fastmcp exposed no team_id argument and the
    call went out with a literal "{team_id}" in the url: a 404 that reads like
    a missing endpoint (2026-11-02, PRI-1221). fixed for the whole class rather
    than that one route, since the spec is hand-assembled upstream and any
    future fragment can repeat it."""
    for path, path_item in spec.get("paths", {}).items():
        template_names = PATH_TEMPLATE_PARAMETER.findall(path)
        if not template_names:
            continue
        shared_parameters = path_item.get("parameters") or []
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            declared = {
                parameter.get("name")
                for parameter in [*shared_parameters, *(operation.get("parameters") or [])]
                if isinstance(parameter, dict) and parameter.get("in") == "path"
            }
            for name in template_names:
                if name not in declared:
                    operation.setdefault("parameters", []).append(
                        {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
                    )


def _accept_create_at_on_scheduled_post_update(spec: dict[str, Any]) -> None:
    """the server validates the whole draft before restoring stored fields
    (app.UpdateScheduledPost: IsValid, then RestoreNonUpdatableFields), so a
    body without a nonzero create_at fails with "Create at must be a valid
    time". the spec omits the field, and mattermost_call drops arguments its
    schema does not list, so the update was unreachable. any nonzero value
    passes; send the post's own create_at and the server keeps it anyway."""
    operation = spec.get("paths", {}).get("/api/v4/posts/schedule/{scheduled_post_id}", {}).get("put")
    if not isinstance(operation, dict):
        return
    schema = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
    if not isinstance(schema, dict):
        return
    properties = schema.setdefault("properties", {})
    properties.setdefault(
        "create_at",
        {"type": "integer", "description": "The scheduled post's create_at (epoch ms) as returned when it was created or listed; required"},
    )
    required = schema.setdefault("required", [])
    if "create_at" not in required:
        required.append("create_at")


def _assign_operation_ids(spec: dict[str, Any]) -> None:
    """every operation gets a unique, stable id.

    an operation with no operationId gets one derived from its summary
    ("Flag a post" -> FlagAPost). v11.5.1 shipped twelve content-flagging
    operations that way; v11.7.14 names every operation upstream (FlagAPost
    became PostCFPostFlag), so this is now a guard for future spec gaps. duplicates are resolved through
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
