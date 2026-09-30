"""Typed settings from the environment.

Env names follow judgy's where one exists (``WANDB_API_KEY``, ``WANDB_ENTITY``,
``WANDB_PROJECT``, ``INFERENCE_BASE_URL``, ``INFERENCE_API_KEY``, ``REVIEWER_MODEL``; judgy
``src/approval_reviewer/config.py`` ENV_FIELD_MAP). Credentials are read from ``*_FILE`` paths
in preference to inline variables, are held as :class:`pydantic.SecretStr`, and never appear in
a repr, a log line or an error message.

Fail closed at startup: a missing facade URL, tenant credential or (in live mode) reviewer
model refuses to start with a :class:`ConfigError` naming the variable and never its value.
A process whose environment carries the AGENT credential refuses too: the judge is a tenant
tool and must never be able to act as the agent it comments on.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

__all__ = [
    "AGENT_CREDENTIAL_ENV_NAMES",
    "DEFAULT_INFERENCE_BASE_URL",
    "ConfigError",
    "InferenceSettings",
    "Settings",
    "load_inference_settings",
    "load_settings",
]

DEFAULT_INFERENCE_BASE_URL = "https://api.inference.wandb.ai/v1"
DEFAULT_WANDB_ENTITY = "bountify"
DEFAULT_WANDB_PROJECT = "judgy"

#: Environment names under which the approval.md AGENT credential is known to travel
#: (approval-md-hosted image/README.md, "Environment": APPROVAL_SERVE_AGENT_TOKEN). Their mere
#: presence in the judge's environment is a deployment error.
AGENT_CREDENTIAL_ENV_NAMES: tuple[str, ...] = (
    "APPROVAL_SERVE_AGENT_TOKEN",
    "AGENT_TOKEN",
    "APPROVAL_AGENT_TOKEN",
)
#: Patterns for the same credential under the names the connect bundle and the gated Hermes
#: image use (``HOSTED_<TENANT>_FACADE_AGENT_TOKEN``, ``APPROVAL_FACADE_TOKEN_ENV`` and kin),
#: and for the approval bot's token (``HOSTED_<TENANT>_TG_BOT_TOKEN``, core's
#: ``APPROVAL_TG_TOKEN``): the judge posts through its OWN bot and must never hold the gate's.
_FORBIDDEN_ENV_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^HOSTED_[A-Z0-9_]+_FACADE_AGENT_TOKEN(_FILE)?$"), "agent credential"),
    (re.compile(r"^APPROVAL_FACADE_TOKEN"), "agent credential"),
    (re.compile(r"^HOSTED_[A-Z0-9_]+_TG_BOT_TOKEN(_FILE)?$"), "approval bot token"),
    (re.compile(r"^APPROVAL_TG_TOKEN(_FILE)?$"), "approval bot token"),
    # The judge's own bot is JUDGE_TG_BOT_TOKEN. The plain legacy name is refused outright:
    # it is ambiguous, and an operator who set it most likely copied the gate's bot.
    (re.compile(r"^TG_BOT_TOKEN(_FILE)?$"), "approval bot token (legacy name TG_BOT_TOKEN)"),
)

_M = TypeVar("_M", bound=BaseModel)

PositiveFloat = Annotated[float, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]


class ConfigError(ValueError):
    """A setting is missing or malformed. The message names variables, never values."""


class InferenceSettings(BaseModel):
    """What the reviewer and Weave need. Enough on its own for ``approved evaluate``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    wandb_entity: str = DEFAULT_WANDB_ENTITY
    wandb_project: str = DEFAULT_WANDB_PROJECT
    wandb_api_key: SecretStr | None = None

    inference_base_url: str = DEFAULT_INFERENCE_BASE_URL
    inference_api_key: SecretStr | None = None
    reviewer_model: str | None = None

    offline: bool = False
    # 60 s: a reasoning model on W&B Inference outlived 25 s on the first live call, which
    # also initialises Weave. The judge is advisory, so a slow verdict delays nothing.
    judge_timeout_s: PositiveFloat = 60.0

    @property
    def weave_project(self) -> str:
        return f"{self.wandb_entity}/{self.wandb_project}"

    def trace_url(self, call_id: str | None) -> str | None:
        """Weave call URL, same form as judgy ``mcp_analysis.py`` ``trace_url``.

        ``None`` when there is no call id: a trace link is never synthesised.
        """
        if not call_id:
            return None
        return f"https://wandb.ai/{self.wandb_entity}/{self.wandb_project}/r/call/{call_id}"


class Settings(InferenceSettings):
    """Frozen runtime configuration for the judge worker."""

    facade_url: str
    tenant_token: SecretStr

    judge_bot_token: SecretStr | None = None
    tg_chat_id: str | None = None
    tg_api_base: str = "https://api.telegram.org"

    state_dir: Path = Path("state")
    policy_file: Path | None = None

    judge_max_age_s: PositiveFloat = 900.0
    poll_interval_s: PositiveFloat = 5.0
    follow_limit: Annotated[int, Field(gt=0, le=1000)] = 200
    #: Must exceed the facade's hook wait (APPROVAL_SERVE_HOOK_TIMEOUT, 12 s recommended):
    #: `approval serve` answers one call at a time, so a follow can queue behind a hook call
    #: that is waiting for a human, and it should be answered when that wait ends.
    http_timeout_s: PositiveFloat = 20.0
    breaker_threshold: PositiveInt = 3
    breaker_cooldown_s: PositiveFloat = 60.0
    feedback_timeout_s: PositiveFloat = 15.0

    #: Operator console (``python -m approved serve``). The token gates every page except
    #: /policy and /health; without one the console starts only in demo mode.
    console_token: SecretStr | None = None
    console_host: str = "0.0.0.0"  # noqa: S104 - a container's listener; the platform fronts it
    console_port: Annotated[int, Field(gt=0, lt=65536)] = 8000
    demo_mode: bool = False
    #: Serve the operator console beside the worker. Off: only /health is served.
    console_enabled: bool = True
    #: Proxies in front of the console that append to X-Forwarded-For (Maritime's public
    #: proxy: 1). 0 uses the socket peer. Only hops we trust are read, from the right.
    trusted_proxy_hops: Annotated[int, Field(ge=0, le=5)] = 0
    #: The path prefix a shared-origin proxy puts in front of the console (Maritime:
    #: ``/a/<agent-id>``). Links and the session cookie's ``Path`` carry it.
    public_base_path: Annotated[str, Field(pattern=r"^(/[A-Za-z0-9._~-]+)*$")] = ""

    @property
    def telegram_enabled(self) -> bool:
        return self.judge_bot_token is not None and bool(self.tg_chat_id)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _secret(env: Mapping[str, str], name: str) -> SecretStr | None:
    """``<NAME>_FILE`` (preferred) or ``<NAME>``. The file wins when both are set."""
    file_name = f"{name}_FILE"
    path = _clean(env.get(file_name))
    if path is not None:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"{file_name} names a file that cannot be read") from exc
        if not value:
            raise ConfigError(f"{file_name} names an empty file")
        return SecretStr(value)
    inline = _clean(env.get(name))
    return SecretStr(inline) if inline is not None else None


def _bool(env: Mapping[str, str], name: str) -> bool:
    raw = (_clean(env.get(name)) or "").lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise ConfigError(f"{name} must be 1/0/true/false")


def _number(env: Mapping[str, str], name: str) -> str | None:
    return _clean(env.get(name))


def _refuse_agent_credential(environ: Mapping[str, str]) -> None:
    """Refuse to start with the agent credential or the approval bot's token in reach."""
    present = [n for n in AGENT_CREDENTIAL_ENV_NAMES if _clean(environ.get(n))]
    present += [n for n in AGENT_CREDENTIAL_ENV_NAMES if _clean(environ.get(f"{n}_FILE"))]
    if present:
        raise ConfigError(
            "the judge's environment carries an agent credential "
            f"({', '.join(sorted(set(present)))}); the judge holds the TENANT credential "
            "only. Remove it from this process's environment."
        )
    for name, value in environ.items():
        if not _clean(value):
            continue
        for pattern, what in _FORBIDDEN_ENV_PATTERNS:
            if pattern.search(name):
                raise ConfigError(
                    f"the judge's environment carries the {what} ({name}); the judge holds the "
                    "TENANT credential and its own bot token (JUDGE_TG_BOT_TOKEN) only."
                )


def _facade_url_allowed(url: str, *, demo: bool, allow_insecure: bool) -> bool:
    """https always; http only in demo mode, on loopback, or, with ALLOW_INSECURE_FACADE=1, to
    a single-label host (a compose service name)."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return bool(parts.hostname)
    if parts.scheme != "http" or not parts.hostname:
        return False
    if demo:
        return True
    host = parts.hostname
    if host == "localhost":
        return True
    if "." not in host and ":" not in host:
        return allow_insecure  # a single-label name (a compose service): opt-in only
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _inference_fields(environ: Mapping[str, str], *, offline: bool) -> dict[str, object]:
    wandb_entity = _clean(environ.get("WANDB_ENTITY")) or DEFAULT_WANDB_ENTITY
    wandb_project = _clean(environ.get("WANDB_PROJECT")) or DEFAULT_WANDB_PROJECT
    if "/" in wandb_project:  # accept the combined "entity/project" spelling
        entity_part, _, project_part = wandb_project.partition("/")
        if not entity_part or not project_part or "/" in project_part:
            raise ConfigError("WANDB_PROJECT must be <project> or <entity>/<project>")
        wandb_entity, wandb_project = entity_part, project_part

    wandb_api_key = _secret(environ, "WANDB_API_KEY")
    inference_api_key = _secret(environ, "INFERENCE_API_KEY") or wandb_api_key
    reviewer_model = _clean(environ.get("REVIEWER_MODEL"))
    if not offline:
        if reviewer_model is None:
            raise ConfigError("REVIEWER_MODEL is required unless OFFLINE=1")
        if inference_api_key is None:
            raise ConfigError(
                "INFERENCE_API_KEY or WANDB_API_KEY (or their _FILE forms) is required "
                "unless OFFLINE=1"
            )
    raw: dict[str, object] = {
        "wandb_entity": wandb_entity,
        "wandb_project": wandb_project,
        "wandb_api_key": wandb_api_key,
        "inference_api_key": inference_api_key,
        "reviewer_model": reviewer_model,
        "offline": offline,
    }
    _optional(environ, raw, {"inference_base_url": "INFERENCE_BASE_URL"})
    _optional(environ, raw, {"judge_timeout_s": "JUDGE_TIMEOUT_S"})
    return raw


def _optional(environ: Mapping[str, str], raw: dict[str, object], names: dict[str, str]) -> None:
    for field_name, env_name in names.items():
        value = _number(environ, env_name)
        if value is not None:
            raw[field_name] = value


def _validate(model: type[_M], raw: dict[str, object]) -> _M:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        # pydantic's message can echo input values; name the fields only.
        fields = sorted({str(err["loc"][0]) for err in exc.errors() if err["loc"]})
        raise ConfigError(f"invalid settings: {', '.join(fields) or 'unknown field'}") from None


def load_inference_settings(
    env: Mapping[str, str] | None = None, *, offline: bool | None = None
) -> InferenceSettings:
    """Reviewer and Weave settings only (``approved evaluate``). ``offline`` overrides OFFLINE."""
    environ: Mapping[str, str] = os.environ if env is None else env
    _refuse_agent_credential(environ)
    is_offline = _bool(environ, "OFFLINE") if offline is None else offline
    return _validate(InferenceSettings, _inference_fields(environ, offline=is_offline))


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build worker :class:`Settings` from ``env`` (default: the process environment)."""
    environ: Mapping[str, str] = os.environ if env is None else env
    _refuse_agent_credential(environ)

    facade_url = _clean(environ.get("FACADE_URL"))
    if facade_url is None:
        raise ConfigError("FACADE_URL is required")
    demo = _bool(environ, "APPROVED_DEMO")
    allow_insecure = _bool(environ, "ALLOW_INSECURE_FACADE")
    if not _facade_url_allowed(facade_url, demo=demo, allow_insecure=allow_insecure):
        raise ConfigError(
            "FACADE_URL must be https (http only for loopback, APPROVED_DEMO=1, or a "
            "single-label host with ALLOW_INSECURE_FACADE=1): the tenant credential and the "
            "log travel on it"
        )

    tenant_token = _secret(environ, "TENANT_TOKEN")
    if tenant_token is None:
        raise ConfigError("TENANT_TOKEN_FILE (preferred) or TENANT_TOKEN is required")

    raw = _inference_fields(environ, offline=_bool(environ, "OFFLINE"))
    policy_file = _clean(environ.get("POLICY_FILE"))
    raw.update(
        {
            "facade_url": facade_url.rstrip("/"),
            "tenant_token": tenant_token,
            "judge_bot_token": _secret(environ, "JUDGE_TG_BOT_TOKEN"),
            "tg_chat_id": _clean(environ.get("TG_CHAT_ID")),
            "policy_file": Path(policy_file) if policy_file else None,
            "console_token": _secret(environ, "CONSOLE_TOKEN"),
            "demo_mode": demo,
            "console_enabled": _clean(environ.get("CONSOLE_ENABLED")) is None
            or _bool(environ, "CONSOLE_ENABLED"),
            "public_base_path": _clean(environ.get("PUBLIC_BASE_PATH")) or "",
        }
    )
    _optional(
        environ,
        raw,
        {
            "tg_api_base": "TG_API_BASE",
            "state_dir": "STATE_DIR",
            "judge_max_age_s": "JUDGE_MAX_AGE_S",
            "poll_interval_s": "POLL_INTERVAL_S",
            "follow_limit": "FOLLOW_LIMIT",
            "http_timeout_s": "HTTP_TIMEOUT_S",
            "breaker_threshold": "BREAKER_THRESHOLD",
            "breaker_cooldown_s": "BREAKER_COOLDOWN_S",
            "feedback_timeout_s": "FEEDBACK_TIMEOUT_S",
            "console_host": "CONSOLE_HOST",
            "trusted_proxy_hops": "TRUSTED_PROXY_HOPS",
            # A platform-injected PORT wins over CONSOLE_PORT (see below).
            "console_port": "CONSOLE_PORT",
        },
    )
    _optional(environ, raw, {"console_port": "PORT"})
    return _validate(Settings, raw)
