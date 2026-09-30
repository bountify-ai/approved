"""The demo's throwaway credentials, generated at container start (``demo/setup.sh`` shapes).

Fresh on every start of the supervisor, never built into the image: two serve credentials,
the webhook secret, the gate's and the judge's fake bot tokens. Each is written to its own file
under ``/data/tryit/secrets`` (directory 0700, files 0600) so the judge and the agent read them
through ``*_FILE`` paths; the daemon, which has no file form, gets them in its environment,
exactly as compose's ``env_file`` gives them. None is a real credential: only the fake
Telegram ever sees a bot token, and every value dies with the container's next start.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Credentials", "generate"]


@dataclass(frozen=True, repr=False)
class Credentials:
    agent_token: str
    tenant_token: str
    webhook_secret: str
    gate_bot_token: str
    judge_bot_token: str

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"

    def daemon_env(self) -> dict[str, str]:
        """As ``demo/.state/daemon.env``: both serve credentials, the webhook secret, the
        gate's bot token."""
        return {
            "APPROVAL_SERVE_AGENT_TOKEN": self.agent_token,
            "APPROVAL_SERVE_TENANT_TOKEN": self.tenant_token,
            "APPROVAL_TG_WEBHOOK_SECRET": self.webhook_secret,
            "HOSTED_DEMO_TG_BOT_TOKEN": self.gate_bot_token,
        }

    def values(self) -> tuple[str, ...]:
        return (
            self.agent_token,
            self.tenant_token,
            self.webhook_secret,
            self.gate_bot_token,
            self.judge_bot_token,
        )


def _write_private(path: Path, value: str) -> None:
    """Write ``value`` to ``path`` as 0600, atomically (temp file, fsync, replace)."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, value.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def generate(directory: Path) -> Credentials:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory.parent, 0o700)
    os.chmod(directory, 0o700)
    creds = Credentials(
        agent_token=secrets.token_hex(24),
        tenant_token=secrets.token_hex(24),
        webhook_secret=secrets.token_hex(24),
        gate_bot_token=f"7001:{secrets.token_hex(18)}",
        judge_bot_token=f"7002:{secrets.token_hex(18)}",
    )
    for name in (
        "agent_token",
        "tenant_token",
        "webhook_secret",
        "gate_bot_token",
        "judge_bot_token",
    ):
        _write_private(directory / name, getattr(creds, name))
    return creds
