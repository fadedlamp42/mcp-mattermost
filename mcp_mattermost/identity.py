"""which sister this machine is, and which mattermost token she speaks with.

the sister is resolved exactly the way sttts resolves its voice: the mac's
IOPlatformUUID is looked up in the `machines` map of sttts' voices.yaml.
that file is the single source of truth for machine -> sister, so it is
read, never copied (a copied table is how the bridge ended up with the
Peters-MacBook-Pro / Peters-MacBook-Pro-2 prefix hazard).

the token for that sister comes from MCP_MATTERMOST_TOKEN_<SISTER> in the
environment, so one committed profile json serves every machine and each
laptop picks its own line. a machine with no sister, or a sister with no
token, refuses to start: no mattermost tools is a safe failure, mattermost
tools posting as the wrong sister is not (PRI-549).
"""

import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_VOICES_MANIFEST = Path.home() / "knowledge/prism/repositories/sttts/voices.yaml"


class IdentityError(RuntimeError):
    """the server cannot establish who it would speak as, so it must not start."""


@dataclass(frozen=True)
class SisterIdentity:
    sister: str
    host_id: str
    token: str


def stable_host_id() -> str:
    """this machine's IOPlatformUUID, the key voices.yaml's machines map uses.

    NOTE: mirrors prism.otel_resource.stable_host_id and sttts' kokoro server.
    falls back to the hostname only when ioreg is unavailable (non-mac), which
    will simply not match the map and fail closed."""
    try:
        report = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        for line in report.splitlines():
            if "IOPlatformUUID" in line:
                return line.split('"')[-2]
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname()


def sister_for_host(host_id: str, manifest_path: Path) -> str:
    """the sister voices.yaml assigns to this host id."""
    if not manifest_path.exists():
        raise IdentityError(f"voices manifest not found at {manifest_path}")
    manifest = yaml.safe_load(manifest_path.read_text())
    machines: dict[str, str] = manifest.get("machines") or {}
    sister: Optional[str] = machines.get(host_id)
    if not sister:
        raise IdentityError(f"host {host_id} is not in the machines map of {manifest_path}; add it there (it is what sttts uses too)")
    return sister


def token_for_sister(sister: str, environment: dict[str, str]) -> str:
    """MCP_MATTERMOST_TOKEN_<SISTER>, refusing a missing or empty value.

    empty matters: opencode's {env:VAR} substitution turns an unset variable
    into "" rather than failing, so an empty token is the shape a broken
    config actually arrives in."""
    variable_name = f"MCP_MATTERMOST_TOKEN_{sister.upper()}"
    token = (environment.get(variable_name) or "").strip()
    if not token:
        raise IdentityError(f"{variable_name} is not set for sister '{sister}'")
    return token


def resolve_identity(environment: Optional[dict[str, str]] = None) -> SisterIdentity:
    """the full chain: host id -> sister -> token. raises IdentityError."""
    environment = dict(os.environ) if environment is None else environment
    manifest_path = Path(environment.get("MCP_MATTERMOST_VOICES_MANIFEST") or DEFAULT_VOICES_MANIFEST)
    host_id = environment.get("MCP_MATTERMOST_HOST_ID") or stable_host_id()
    sister = sister_for_host(host_id, manifest_path)
    return SisterIdentity(sister=sister, host_id=host_id, token=token_for_sister(sister, environment))


def assert_token_speaks_as_sister(identity: SisterIdentity, authenticated_username: str) -> None:
    """the token must authenticate as the sister the machine is.

    this is the check that makes misattribution impossible rather than
    unlikely: a lily token pasted into rose's variable fails at startup
    instead of quietly posting as the wrong sister."""
    if authenticated_username != identity.sister:
        raise IdentityError(f"this machine is {identity.sister} but its token authenticates as '{authenticated_username}'")
