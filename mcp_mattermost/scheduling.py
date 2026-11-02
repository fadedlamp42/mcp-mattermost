"""scheduled posts: say something later, including "wake me up later".

mattermost holds a scheduled post server-side and a job sends it as a normal
post from the scheduling account. on our server that job only runs because
the prism build carries a self-issued license (PRI-1221); an unlicensed
server answers api.scheduled_posts.license_error.

the reason these are core tools rather than generic calls is everything the
raw api leaves to the caller (all hit by hand on 2026-11-02):
  - times are epoch milliseconds; here they are ISO 8601 with an explicit
    offset, and a time with no offset is refused rather than guessed, since a
    reminder silently shifted by eight hours is worse than an error
  - the sender job runs every 5 minutes, so a post lands up to ~5 minutes
    after its time; the descriptions say so instead of letting it look broken
  - an update must resend the whole draft, create_at included, or the
    server rejects it; the update tool fetches and resends it
  - "wake me later" is a post that mentions its own author: the sisters'
    bridge treats a mention from a trusted agent as a prompt, so the post
    opens a fresh session when it fires (verified 2026-11-02, @lily say hi)
"""

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Optional

from fastmcp import FastMCP
from pydantic import Field

from .api import Mattermost, MattermostError, post_priority, readable_timestamps
from .core_tools import PERSISTENT_NOTIFICATIONS_DESCRIPTION, PRIORITY_DESCRIPTION, REQUESTED_ACK_DESCRIPTION, PriorityLevel

# the server accepts times up to 5 s in the past (scheduledPostMaxTimeGap);
# anything older is a mistake worth naming before the server's generic error
ALLOWED_PAST_MILLISECONDS = 5_000
SENDER_INTERVAL_NOTE = "The server's sender runs every 5 minutes, so the post appears up to ~5 minutes after this time."

AT_DESCRIPTION = (
    "When to send, as ISO 8601 with an explicit UTC offset, e.g. '2026-11-09T09:00:00-08:00' or '2026-11-09T17:00:00Z'. "
    "A time without an offset is refused. " + SENDER_INTERVAL_NOTE
)
WAKE_DESCRIPTION = (
    "Prefix the message with an @mention of this account, so that when it fires the sisters' bridge opens a new session "
    "for it: 'remind myself later'. Yes, this works even though this account is the author: the bridge deliberately "
    "does not filter a sister's own posts, only unaddressed ones (proven 2026-11-02: "
    "https://mattermost.prism-dynamics.org/prism-dynamics/pl/6dgkkuydgb853pzka4owi3wbxy). "
    "Write the message as instructions to that future session"
)


ISSUE_MENTION = re.compile(r"\b([A-Z]{2,6}-\d+)\b")


def dossier_reminder(post_id: str, message: str) -> str:
    """the line a schedule result carries so the scheduling session links the
    post into the supervision dossier without being told to (peter,
    2026-11-02: "an automatic mechanism, so I never have to remind sisters").

    it rides in the RESULT rather than only the description because the
    result arrives at the one moment the session holds the id and is
    choosing its next step. the issue ids the message names are offered as
    candidates, never applied: a post can name an issue it is not about.
    """
    named = list(dict.fromkeys(ISSUE_MENTION.findall(message)))
    call = (
        'prism_supervision_record(issue_id=<issue>, kind="mattermost_scheduled_post", '
        'value="%(id)s", session_id=<your session id>)' % {"id": post_id}
    )
    candidates = (
        " The message names %(ids)s; link the ones it serves." % {"ids": ", ".join(named)}
        if named
        else " It names no issue id, so link the issue this session is working on."
    )
    return (
        "Record this post in the supervision dossier now: %(call)s, once per issue it serves "
        "(one post may be linked to several issues).%(candidates)s Linked, it shows as a wake "
        "on each issue's cockpit row; unlinked, it waits on the cockpit's unlinked-wakes shelf. "
        "Skip this only when the post serves no tracked issue."
        % {"call": call, "candidates": candidates}
    )


def epoch_milliseconds_from_iso(at: str, now: Optional[datetime] = None) -> int:
    """'2026-11-09T09:00:00-08:00' -> epoch ms, refusing naive or past times."""
    try:
        moment = datetime.fromisoformat(at.strip())
    except ValueError as error:
        raise MattermostError(f"'{at}' is not an ISO 8601 time: {error}") from error
    if moment.tzinfo is None:
        raise MattermostError(f"'{at}' has no UTC offset; add one (e.g. -08:00 for PST, Z for UTC) so the time is unambiguous")
    milliseconds = int(moment.timestamp() * 1000)
    now_milliseconds = int((now or datetime.now(UTC)).timestamp() * 1000)
    if milliseconds < now_milliseconds - ALLOWED_PAST_MILLISECONDS:
        raise MattermostError(f"'{at}' is in the past ({moment.astimezone(UTC).isoformat()} UTC)")
    return milliseconds


def with_self_mention(message: str, username: str) -> str:
    """the message with a leading @username, unless it already mentions it."""
    mention = f"@{username}"
    if mention.lower() in message.lower():
        return message
    return f"{mention} {message}"


def replacement_message(current_message: str, new_message: str, username: str, wake: Optional[bool]) -> str:
    """the text an update sends. wake=None keeps the post's current behaviour:
    rewriting a wake-up's text without its mention would silently turn it into
    a post nobody acts on, which is the failure the self-mention exists to
    prevent (found live, 2026-11-02)."""
    already_wakes = f"@{username}".lower() in current_message.lower()
    should_wake = already_wakes if wake is None else wake
    return with_self_mention(new_message, username) if should_wake else new_message


def flatten_scheduled_posts(response: Any) -> list[dict[str, Any]]:
    """the list endpoint answers {<team id or "directChannels">: [posts]};
    callers want one list in send order."""
    groups = response.values() if isinstance(response, dict) else []
    posts = [post for group in groups if isinstance(group, list) for post in group]
    return sorted(posts, key=lambda post: post.get("scheduled_at") or 0)


async def fetch_scheduled_posts(mattermost: Mattermost) -> list[dict[str, Any]]:
    response = await mattermost.request(
        "GET",
        f"/api/v4/posts/scheduled/team/{mattermost.team_id}",
        params={"includeDirectChannels": "true"},
    )
    return flatten_scheduled_posts(response)


def register_scheduling_tools(mcp: FastMCP, mattermost: Mattermost) -> None:
    @mcp.tool(name="mattermost_schedule_post")
    async def schedule_post(
        channelId: Annotated[str, Field(description="Channel ID to post in")],
        message: Annotated[str, Field(description="Message content (markdown)")],
        at: Annotated[str, Field(description=AT_DESCRIPTION)],
        rootId: Annotated[Optional[str], Field(description="Post ID to reply to, to send it into a thread")] = None,
        wake: Annotated[bool, Field(description=WAKE_DESCRIPTION)] = False,
        priority: Annotated[Optional[PriorityLevel], Field(description=PRIORITY_DESCRIPTION)] = None,
        requestedAck: Annotated[bool, Field(description=REQUESTED_ACK_DESCRIPTION)] = False,
        persistentNotifications: Annotated[bool, Field(description=PERSISTENT_NOTIFICATIONS_DESCRIPTION)] = False,
    ) -> dict[str, Any]:
        """Schedule a post to be sent later as this account. Use it for reminders, follow-up checks, and (with wake=true) for waking a fresh agent session at a future time. Returns the scheduled post with its id; list, update, or cancel it with the other mattermost_*scheduled_post* tools. When it serves a tracked issue, record that id with prism_supervision_record(issue_id, kind="mattermost_scheduled_post", value=<id>) on EACH issue it serves (one post may be linked to several), so the cockpit shows the wake on those rows; the result's `next` field spells out the exact call. An unrecorded one lands on the cockpit's unlinked-wakes shelf instead."""
        body: dict[str, Any] = {
            "channel_id": channelId,
            "message": with_self_mention(message, mattermost.me["username"]) if wake else message,
            "scheduled_at": epoch_milliseconds_from_iso(at),
        }
        if rootId:
            body["root_id"] = rootId
        priority_fields = post_priority(priority, requestedAck, persistentNotifications)
        if priority_fields:
            body["priority"] = priority_fields
        scheduled = await mattermost.request("POST", "/api/v4/posts/schedule", json=body)
        return {
            **readable_timestamps(scheduled),
            "note": SENDER_INTERVAL_NOTE,
            "next": dossier_reminder(scheduled.get("id") or "", body["message"]),
        }

    @mcp.tool(name="mattermost_list_scheduled_posts")
    async def list_scheduled_posts() -> list[dict[str, Any]]:
        """This account's pending scheduled posts in this team (channels and DMs), soonest first. A nonempty error_code means the server tried and failed to send it."""
        return [readable_timestamps(post) for post in await fetch_scheduled_posts(mattermost)]

    @mcp.tool(name="mattermost_update_scheduled_post")
    async def update_scheduled_post(
        scheduledPostId: Annotated[
            str, Field(description="Scheduled post ID, from mattermost_schedule_post or mattermost_list_scheduled_posts")
        ],
        message: Annotated[Optional[str], Field(description="New message content; omit to keep the current one")] = None,
        at: Annotated[Optional[str], Field(description="New send time; omit to keep the current one. " + AT_DESCRIPTION)] = None,
        wake: Annotated[
            Optional[bool],
            Field(
                description="Whether the new message mentions this account (see mattermost_schedule_post). "
                "Omit to keep what the post already does: a wake-up stays a wake-up"
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Change the message or send time of one of this account's pending scheduled posts, keeping everything else (channel, thread, priority, and whether it wakes a session) as it was."""
        if message is None and at is None:
            raise MattermostError("nothing to update: pass message, at, or both")
        pending = await fetch_scheduled_posts(mattermost)
        existing = next((post for post in pending if post.get("id") == scheduledPostId), None)
        if existing is None:
            raise MattermostError(
                f"no pending scheduled post {scheduledPostId} for this account (already sent, cancelled, or another account's)"
            )
        # the server validates the whole draft before restoring stored fields,
        # so the existing post goes back intact with only the requested changes
        body = dict(existing)
        if message is not None:
            body["message"] = replacement_message(existing.get("message") or "", message, mattermost.me["username"], wake)
        if at is not None:
            body["scheduled_at"] = epoch_milliseconds_from_iso(at)
        updated = await mattermost.request("PUT", f"/api/v4/posts/schedule/{scheduledPostId}", json=body)
        return {**readable_timestamps(updated), "note": SENDER_INTERVAL_NOTE}

    @mcp.tool(name="mattermost_cancel_scheduled_post")
    async def cancel_scheduled_post(
        scheduledPostId: Annotated[str, Field(description="Scheduled post ID to cancel; it will never be sent")],
    ) -> dict[str, Any]:
        """Cancel one of this account's pending scheduled posts. Returns the cancelled post so its content is not lost."""
        return readable_timestamps(await mattermost.request("DELETE", f"/api/v4/posts/schedule/{scheduledPostId}"))
