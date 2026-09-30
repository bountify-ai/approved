"""The try-it image's layout and settings.

Everything a child is started with is built here, from constants and a few environment
knobs, so the supervisor never passes its own environment through: each child gets exactly
the variables the demo's compose file gives the matching service (``demo/compose.yaml``,
kept in step by ``tests/test_tryit.py``), with service names replaced by loopback addresses.

Interpreters and scripts are ABSOLUTE paths. Maritime's init re-launches the image command
with its own PATH, and a bare ``python`` resolved to the system interpreter there
(2026-09-29), so no child here is ever found through PATH.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "AGENT_PYTHON",
    "APPROVAL_CLI",
    "DAEMON_COMPOSE_ENV",
    "FAKE_TG_COMPOSE_ENV",
    "LIVE_PASSTHROUGH",
    "Layout",
    "Settings",
    "TryitConfigError",
    "load_settings",
]

# ---------------------------------------------------------------- image paths (absolute)

NODE = "/usr/local/bin/node"
JUDGE_PYTHON = "/app/.venv/bin/python"
AGENT_PYTHON = "/usr/local/bin/python3"
SH = "/bin/sh"
APPROVAL_CLI = "/opt/runtime/node_modules/approval-md/cli.js"
DAEMON_ENTRYPOINT = "/opt/image/entrypoint.mjs"
LOOPBACK_PRELOAD = "file:///opt/tryit/loopback.mjs"
FAKE_TELEGRAM = "/demo/fake-telegram/server.mjs"
AGENT_SCRIPT = "/demo/agent/agent.py"
INIT_SCRIPT = "/demo/init.sh"
#: The PATH every child gets. Nothing is resolved through it by this package; it is there for
#: the demo's init step, which calls ``node`` by name.
CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"

# ---------------------------------------------------------------- loopback ports

DAEMON_PORT = 8080  # the daemon image's supervisor ($PORT inside that program)
FAKE_TG_PORT = 8081
JUDGE_PORT = 8000
#: Ports the children hold; the front's $PORT may be none of them. 4682-4685 are the daemon
#: image's internal `approval serve` and webhook ports.
RESERVED_PORTS = frozenset(
    {DAEMON_PORT, FAKE_TG_PORT, JUDGE_PORT, 4682, 4683, 4684, 4685}
    | set(range(18000, 18005))
    | set(range(18100, 18105))
)

DEMO_CHAT_ID = "4242"
TENANT = "demo"

# ---------------------------------------------------------------- the compose file's env

#: ``demo/compose.yaml``, service ``daemon``, ``environment:``, verbatim.
DAEMON_COMPOSE_ENV: dict[str, str] = {
    "PORT": "8080",
    "APPROVAL_TENANT": "demo",
    "APPROVAL_AGENT": "agent:hermes-demo",
    "APPROVAL_DAEMON_ID": "daemon-demo-1",
    "APPROVAL_HUMAN": "human:demo",
    "HOSTED_DEMO_TG_CHAT": "4242",
    "APPROVAL_SERVE_HOOK_HARNESS_CAP": "300s",
    "APPROVAL_SERVE_HOOK_TIMEOUT": "10s",
    "APPROVAL_PUBLIC_URL": "https://daemon.approved-demo.invalid",
    "APPROVAL_IMAGE_TG_API_BASE": "http://fake-telegram:8081",
    "APPROVAL_TICK_CRON": "off",
}
#: ``demo/compose.yaml``, service ``fake-telegram``, ``environment:``, verbatim.
FAKE_TG_COMPOSE_ENV: dict[str, str] = {
    "FAKE_TG_PORT": "8081",
    "DEMO_CHAT_ID": "4242",
    "DEMO_SENDER_ID": "4242",
    "FORWARD_WEBHOOK_URL": "http://daemon:8080/telegram/webhook",
}
#: Compose service names become loopback addresses; nothing else changes.
_LOOPBACK = {
    "http://fake-telegram:8081": f"http://127.0.0.1:{FAKE_TG_PORT}",
    "http://daemon:8080": f"http://127.0.0.1:{DAEMON_PORT}",
}

#: The daemon image's own ENV lines (images/daemon/Dockerfile), which its entrypoint and the
#: demo's init step read.
_DAEMON_IMAGE_ENV = {
    "NODE_ENV": "production",
    "APPROVAL_CLI": APPROVAL_CLI,
    "APPROVAL_DATA_DIR": "/data",
}

#: Variables passed from the machine environment to the JUDGE ONLY, and only in live mode:
#: what ``approved serve`` reads for the live reviewer and Weave (``approved/config.py``).
#: ``WANDB_BASE_URL`` and ``WEAVE_DISABLED`` are the W&B client's own switches.
LIVE_PASSTHROUGH: tuple[str, ...] = (
    "WANDB_API_KEY",
    "WANDB_API_KEY_FILE",
    "WANDB_ENTITY",
    "WANDB_PROJECT",
    "WANDB_BASE_URL",
    "WEAVE_DISABLED",
    "INFERENCE_API_KEY",
    "INFERENCE_API_KEY_FILE",
    "INFERENCE_BASE_URL",
    "REVIEWER_MODEL",
    "JUDGE_TIMEOUT_S",
)

_ORIGIN = re.compile(r"^https://[a-z0-9.-]+(:[0-9]{1,5})?$")
_BASE_PATH = re.compile(r"^(/[A-Za-z0-9._~-]+)*$")


class TryitConfigError(ValueError):
    """A setting is missing or malformed. The message names the variable, never a value."""


@dataclass(frozen=True)
class Layout:
    """Where state lives. The demo's init step hardcodes ``/data/demo``, so the root is fixed
    in the image; tests point it elsewhere."""

    data: Path = Path("/data")

    @property
    def store(self) -> Path:
        return self.data / TENANT

    @property
    def judge_state(self) -> Path:
        return self.data / "judge"

    @property
    def tryit(self) -> Path:
        return self.data / "tryit"

    @property
    def secrets(self) -> Path:
        return self.tryit / "secrets"

    @property
    def budget_file(self) -> Path:
        return self.tryit / "live-budget.json"


@dataclass(frozen=True)
class Settings:
    port: int
    daemon_port: int = DAEMON_PORT
    fake_tg_port: int = FAKE_TG_PORT
    judge_port: int = JUDGE_PORT
    serve_port: int = 4682
    webhook_port: int = 4683
    #: The harness cap the daemon's facade is told (``--hook-harness-cap``). The approval
    #: window is this minus the runtime's 60-second margin: 300 s (the demo's) gives 240 s.
    harness_cap_s: int = 300
    run_min_interval_s: float = 20.0
    runs_per_hour: int = 60
    #: TRYIT_LIVE=1 was asked for. ``live_ready`` says whether it can be honoured.
    live_requested: bool = False
    live_ready: bool = False
    live_not_ready_reason: str | None = None
    live_daily_cap: int = 300
    live_calls_per_hour: int = 20
    live_lifetime_cap: int = 80
    inference_budget_file: Path = Path("/data/tryit/inference-calls.json")
    reviewer_model: str | None = None
    weave_project: str = "bountify/judgy"
    frame_ancestors: tuple[str, ...] = ("https://approval.md",)
    public_base_path: str = ""
    #: The live variables to hand the judge. Values never appear in a repr.
    live_env: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def window_s(self) -> int:
        return self.harness_cap_s - 60

    @property
    def agent_wait_s(self) -> int:
        """How long one scenario's agent re-asks: past the approval window plus the daemon's
        30-second expiry sweep, so the GATE's expiry always ends an unanswered request and
        no request is left open when the agent exits."""
        return self.harness_cap_s + 30

    def daemon_env(
        self, credentials: Mapping[str, str], layout: Layout | None = None
    ) -> dict[str, str]:
        env = {k: _LOOPBACK.get(v, v) for k, v in DAEMON_COMPOSE_ENV.items()}
        env["PORT"] = str(self.daemon_port)
        env["APPROVAL_IMAGE_TG_API_BASE"] = f"http://127.0.0.1:{self.fake_tg_port}"
        env["APPROVAL_SERVE_INTERNAL_PORT"] = str(self.serve_port)
        env["APPROVAL_WEBHOOK_INTERNAL_PORT"] = str(self.webhook_port)
        env["APPROVAL_SERVE_HOOK_HARNESS_CAP"] = f"{self.harness_cap_s}s"
        env.update(_DAEMON_IMAGE_ENV)
        if layout is not None:
            env["APPROVAL_DATA_DIR"] = str(layout.data)
        env["PATH"] = CHILD_PATH
        env.update(credentials)
        return env

    def fake_tg_env(self) -> dict[str, str]:
        env = dict(FAKE_TG_COMPOSE_ENV)
        env["FAKE_TG_PORT"] = str(self.fake_tg_port)
        env["FORWARD_WEBHOOK_URL"] = f"http://127.0.0.1:{self.daemon_port}/telegram/webhook"
        env["PATH"] = CHILD_PATH
        return env

    def judge_env(self, layout: Layout, *, live: bool) -> dict[str, str]:
        env = {
            "PATH": CHILD_PATH,
            "HOME": "/tmp",  # noqa: S108 - the judge writes nothing there; libraries want a HOME
            "PYTHONUNBUFFERED": "1",
            "WANDB_ERROR_REPORTING": "false",
            "APPROVED_DEMO": "1",
            "CONSOLE_ENABLED": "0",
            "CONSOLE_HOST": "127.0.0.1",
            "CONSOLE_PORT": str(self.judge_port),
            "FACADE_URL": f"http://127.0.0.1:{self.daemon_port}",
            "TG_API_BASE": f"http://127.0.0.1:{self.fake_tg_port}",
            "TG_CHAT_ID": DEMO_CHAT_ID,
            "STATE_DIR": str(layout.judge_state),
            "POLL_INTERVAL_S": "1",
            "TENANT_TOKEN_FILE": str(layout.secrets / "tenant_token"),
            "JUDGE_TG_BOT_TOKEN_FILE": str(layout.secrets / "judge_bot_token"),
            "OFFLINE": "0" if live else "1",
        }
        env["INFERENCE_CALL_BUDGET_FILE"] = str(self.inference_budget_file)
        env["INFERENCE_CALL_BUDGET_LIMIT"] = str(self.live_lifetime_cap)
        env["INFERENCE_CALL_BUDGET_HOURLY_LIMIT"] = str(self.live_calls_per_hour)
        if live:
            env.update(self.live_env)
        return env

    def agent_env(self, layout: Layout, *, run_id: str, scenario: str) -> dict[str, str]:
        return {
            "PATH": CHILD_PATH,
            "PYTHONUNBUFFERED": "1",
            "FACADE_URL": f"http://127.0.0.1:{self.daemon_port}",
            "AGENT_TOKEN_FILE": str(layout.secrets / "agent_token"),
            "AGENT_WAIT_S": str(self.agent_wait_s),
            "AGENT_SCENARIOS": scenario,
            "AGENT_RUN_ID": run_id,
            "AGENT_STORE_DIR": str(layout.store),
        }


def _clean(env: Mapping[str, str], name: str) -> str | None:
    value = (env.get(name) or "").strip()
    return value or None


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = _clean(env, name)
    if raw is None:
        return default
    if not re.fullmatch(r"[0-9]{1,7}", raw) or not lo <= int(raw) <= hi:
        raise TryitConfigError(f"{name} must be a whole number from {lo} to {hi}")
    return int(raw)


def _bool(env: Mapping[str, str], name: str) -> bool:
    raw = (_clean(env, name) or "").lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise TryitConfigError(f"{name} must be 1/0/true/false")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    environ: Mapping[str, str] = os.environ if env is None else env
    port = _int(environ, "PORT", -1, 1, 65535)
    if port == -1:
        raise TryitConfigError("PORT is unset: the platform injects it (pass -e PORT=<n> by hand)")
    if port in RESERVED_PORTS:
        raise TryitConfigError(
            f"PORT is one of the ports the children hold on loopback ({sorted(RESERVED_PORTS)})"
        )

    live_requested = _bool(environ, "TRYIT_LIVE")
    has_key = any(
        _clean(environ, n)
        for n in (
            "WANDB_API_KEY",
            "WANDB_API_KEY_FILE",
            "INFERENCE_API_KEY",
            "INFERENCE_API_KEY_FILE",
        )
    )
    model = _clean(environ, "REVIEWER_MODEL")
    reason = None
    if live_requested and not has_key:
        reason = "TRYIT_LIVE=1 but no WANDB_API_KEY (or INFERENCE_API_KEY) is set"
    elif live_requested and model is None:
        reason = "TRYIT_LIVE=1 but REVIEWER_MODEL is unset"
    live_ready = live_requested and reason is None

    entity = _clean(environ, "WANDB_ENTITY") or "bountify"
    project = _clean(environ, "WANDB_PROJECT") or "judgy"
    weave_project = project if "/" in project else f"{entity}/{project}"

    ancestors_raw = _clean(environ, "TRYIT_FRAME_ANCESTORS")
    ancestors = tuple(ancestors_raw.split()) if ancestors_raw else ("https://approval.md",)
    if not all(_ORIGIN.match(a) for a in ancestors):
        raise TryitConfigError(
            "TRYIT_FRAME_ANCESTORS must be space-separated https origins (no paths, no wildcards)"
        )
    base = _clean(environ, "PUBLIC_BASE_PATH") or ""
    if not _BASE_PATH.match(base):
        raise TryitConfigError("PUBLIC_BASE_PATH must look like /a/<agent-id> (or be unset)")

    min_interval = _int(environ, "TRYIT_RUN_MIN_INTERVAL_S", 20, 0, 86_400)
    return Settings(
        port=port,
        harness_cap_s=_int(environ, "TRYIT_HOOK_HARNESS_CAP_S", 300, 90, 300),
        run_min_interval_s=float(min_interval),
        runs_per_hour=_int(environ, "TRYIT_RUNS_PER_HOUR", 60, 1, 10_000),
        live_requested=live_requested,
        live_ready=live_ready,
        live_not_ready_reason=reason,
        live_daily_cap=_int(environ, "TRYIT_LIVE_DAILY_CAP", 80, 0, 80),
        live_calls_per_hour=_int(environ, "TRYIT_LIVE_CALLS_PER_HOUR", 20, 1, 20),
        live_lifetime_cap=_int(environ, "TRYIT_LIVE_LIFETIME_CAP", 80, 1, 80),
        reviewer_model=model,
        weave_project=weave_project,
        frame_ancestors=ancestors,
        public_base_path=base,
        live_env={n: environ[n] for n in LIVE_PASSTHROUGH if _clean(environ, n)},
    )
