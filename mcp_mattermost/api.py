"""the one http client every tool shares, plus the few helpers the typed core needs.

mattermost answers errors as json ({"id", "message", "status_code"}); those
are raised as MattermostError carrying the server's own message, so an agent
sees "you do not have the appropriate permissions" rather than a bare 403.
"""

import mimetypes
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import httpx

TIMESTAMP_FIELDS = ("create_at", "update_at", "edit_at", "delete_at", "last_post_at", "last_reply_at")


class MattermostError(RuntimeError):
    pass


@dataclass
class Mattermost:
    client: httpx.AsyncClient
    team_name: str
    team_id: str = ""
    me: dict[str, Any] = field(default_factory=dict)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise MattermostError(_describe_error(response))
        if not response.content:
            return None
        if "application/json" in response.headers.get("content-type", ""):
            return response.json()
        return response.content

    async def initialize(self) -> None:
        """who we are and which team we act in; both are needed by nearly every core tool."""
        self.me = await self.request("GET", "/api/v4/users/me")
        team = await self.request("GET", f"/api/v4/teams/name/{self.team_name}")
        self.team_id = team["id"]

    async def upload_files(self, channel_id: str, file_paths: list[str]) -> list[str]:
        """upload local files into a channel; returns their file ids for a post.

        the api accepts the bytes as multipart, which an agent cannot pass
        through a json tool argument; reading the path here is the whole
        reason file transfer is hand-written rather than generated."""
        file_ids: list[str] = []
        for file_path in file_paths:
            local_file = Path(file_path).expanduser()
            if not local_file.is_file():
                raise MattermostError(f"no file at {local_file}")
            content_type = mimetypes.guess_type(local_file.name)[0] or "application/octet-stream"
            with local_file.open("rb") as handle:
                uploaded = await self.request(
                    "POST",
                    "/api/v4/files",
                    data={"channel_id": channel_id},
                    files={"files": (local_file.name, handle, content_type)},
                )
            file_ids.extend(info["id"] for info in uploaded["file_infos"])
        return file_ids

    async def download_file(self, file_id: str, directory: Path) -> dict[str, Any]:
        """write an attachment to disk and return where it went plus its metadata."""
        info = await self.request("GET", f"/api/v4/files/{file_id}/info")
        content = await self.request("GET", f"/api/v4/files/{file_id}")
        target_directory = directory.expanduser() / file_id
        target_directory.mkdir(parents=True, exist_ok=True)
        target = target_directory / Path(info["name"]).name
        target.write_bytes(content if isinstance(content, bytes) else bytes(str(content), "utf-8"))
        return {
            "path": str(target),
            "name": info["name"],
            "mime_type": info.get("mime_type"),
            "size": info.get("size"),
            "post_id": info.get("post_id"),
            "channel_id": info.get("channel_id"),
        }


def _describe_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        message = body.get("message") or body
    except ValueError:
        message = response.text[:500]
    return f"{response.request.method} {response.request.url.path} -> {response.status_code}: {message}"


def readable_timestamps(entity: dict[str, Any]) -> dict[str, Any]:
    """epoch milliseconds -> ISO 8601 UTC; zero ("never") -> None.

    the old server rendered zero as "", which reads as a value; None says
    the event has not happened."""
    converted = dict(entity)
    for field_name in TIMESTAMP_FIELDS:
        value = converted.get(field_name)
        if isinstance(value, int):
            converted[field_name] = datetime.fromtimestamp(value / 1000, tz=UTC).isoformat() if value else None
    return converted


def readable_post_list(post_list: dict[str, Any]) -> dict[str, Any]:
    """a PostList with its posts in `order` order and readable timestamps."""
    posts = post_list.get("posts") or {}
    order: list[str] = post_list.get("order") or []
    return {
        **post_list,
        "posts": {post_id: readable_timestamps(posts[post_id]) for post_id in order if post_id in posts},
    }


def split_identifiers(value: Optional[str]) -> list[str]:
    """the comma-separated id lists the original tools accepted."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]
