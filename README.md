A Mattermost MCP server: a typed core for daily work, plus `find` / `describe` / `call` over the server's entire REST v4 API (528 operations at v11.5.1).

Forked from [dakatan/mcp-mattermost](https://github.com/dakatan/mcp-mattermost) and rewritten in Python on FastMCP per `~/knowledge/engineering/passages/blueprints/mcp.md` (PRI-549, PRI-923).

`ses_ef964fcb3ffeRfjI0g3SFBbsDf`

## why not one tool per operation

The blueprint's `FastMCP.from_openapi` exposes every operation as a tool. For Mattermost that is 528 tools and **~238k tokens of tool definitions**, loaded into every session, because opencode loads MCP tool schemas eagerly. So the 528 operations are built into a private FastMCP server that is never exposed, and three tools reach it. The whole surface is 21 tools, ~3.1k tokens (measured 2026-10-04).

| tools | what |
| --- | --- |
| `mattermost_create_post`, `_get_posts`, `_get_posts_thread`, `_get_posts_unread`, `_search_posts`, `_pin_post`, `_unpin_post`, `_get_pinned_posts`, `_get_channels`, `_get_my_channels`, `_search_channels`, `_get_users`, `_search_users`, `_add_reaction`, `_remove_reaction`, `_get_reactions` | the original 16, same names and arguments, so existing prompts keep working |
| `mattermost_create_post(filePaths=[...])` | attach local files to a post |
| `mattermost_download_file` | write an attachment to disk and return its path |
| `mattermost_whoami` | the sister this machine is and the account its token authenticates as |
| `mattermost_find_operation` → `mattermost_describe_operation` → `mattermost_call` | everything else in the API. `call` refuses DELETE operations unless `confirmDelete=true` |

Timestamps in the typed core are ISO 8601 UTC, and "never" (Mattermost's `0`) is `null`. The generic `call` returns Mattermost's raw json.

## identity

One committed profile serves every laptop. The server reads this Mac's IOPlatformUUID and looks it up in the `machines` map of `~/knowledge/prism/repositories/sttts/voices.yaml`, the same way sttts picks its voice. Then it takes the token from `MCP_MATTERMOST_TOKEN_<SISTER>` and checks that `/users/me` returns that sister's username.

Each of these makes the server refuse to start, so the session just has no Mattermost tools rather than posting as the wrong sister:

- the machine is not in the machines map
- the sister has no token, or an empty one (opencode turns an unset `{env:VAR}` into `""`)
- the token belongs to another account

## environment

- `MCP_MATTERMOST_URL` (required), e.g. `https://mattermost.prism-dynamics.org`
- `MCP_MATTERMOST_TEAM_NAME` (required), the team the core tools act in
- `MCP_MATTERMOST_TOKEN_ROSE`, `MCP_MATTERMOST_TOKEN_LILY`, ... one per sister
- `MCP_MATTERMOST_VOICES_MANIFEST` (optional), overrides the voices.yaml path

The profile lives at `faded-setup/mcp-servers/mattermost.json`. It runs this repo's own uv environment, so run `uv sync` once per machine.

## the spec

`openapi.yaml` is built from Mattermost's own sources at the tag in `openapi.version`. Mattermost no longer publishes a compiled spec, so `scripts/build-spec.sh` concatenates the fragments the same way their Makefile does. **Pin the tag to the server's version** (`GET /api/v4/config/client?format=old` → `Version`), never master: master documents endpoints an older server 404s on.

```sh
scripts/build-spec.sh v11.5.1
uv run pytest -q
```

`mcp_mattermost/spec.py` patches the spec where it is wrong:

- one misindented response (`custom_profile_attributes`) that FastMCP refuses to parse
- twelve content-flagging operations with no `operationId`, which get one derived from their summary
- a duplicate `SearchFiles` id, which is renamed

Any duplicate left after that fails at startup.

## development

```sh
uv sync
uv run pytest -q
uv run ruff check mcp_mattermost tests
```

# tasks

## todo
## ongoing
## done
## cancelled
