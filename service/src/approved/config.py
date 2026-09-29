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

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, TypeVar

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
    judge_timeout_s: PositiveFloat = 25.0

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

    tg_bot_token: SecretStr | None = None
    tg_chat_id: str | None = None
    tg_api_base: str = "https://api.telegram.org"

    state_dir: Path = Path("state")
    policy_file: Path | None = None

    judge_max_age_s: PositiveFloat = 900.0
    poll_interval_s: PositiveFloat = 5.0
    follow_limit: Annotated[int, Field(gt=0, le=1000)] = 200
    http_timeout_s: PositiveFloat = 10.0
    breaker_threshold: PositiveInt = 3
    breaker_cooldown_s: PositiveFloat = 60.0
    feedback_timeout_s: PositiveFloat = 15.0

    #: Operator console (``python -m approved serve``). The token gates every page except
    #: /policy and /health; without one the console starts only in demo mode.
    console_token: SecretStr | None = None
    console_host: str = "0.0.0.0"  # noqa: S104 - a container's listener; the platform fronts it
    console_port: Annotated[int, Field(gt=0, lt=65536)] = 8000
    demo_mode: bool = False

    @property
    def telegram_enabled(self) -> bool:
        return self.tg_bot_token is not None and bool(self.tg_chat_id)


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
    present = [n for n in AGENT_CREDENTIAL_ENV_NAMES if _clean(environ.get(n))]
    present += [n for n in AGENT_CREDENTIAL_ENV_NAMES if _clean(environ.get(f"{n}_FILE"))]
    if present:
        raise ConfigError(
            "the judge's environment carries an agent credential "
            f"({', '.join(sorted(set(present)))}); the judge holds the TENANT credential "
            "only. Remove it from this process's environment."
        )


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
    if not facade_url.startswith(("https://", "http://")):
        raise ConfigError("FACADE_URL must be an http(s) URL")

    tenant_token = _secret(environ, "TENANT_TOKEN")
    if tenant_token is None:
        raise ConfigError("TENANT_TOKEN_FILE (preferred) or TENANT_TOKEN is required")

    raw = _inference_fields(environ, offline=_bool(environ, "OFFLINE"))
    policy_file = _clean(environ.get("POLICY_FILE"))
    raw.update(
        {
            "facade_url": facade_url.rstrip("/"),
            "tenant_token": tenant_token,
            "tg_bot_token": _secret(environ, "TG_BOT_TOKEN"),
            "tg_chat_id": _clean(environ.get("TG_CHAT_ID")),
            "policy_file": Path(policy_file) if policy_file else None,
            "console_token": _secret(environ, "CONSOLE_TOKEN"),
            "demo_mode": _bool(environ, "APPROVED_DEMO"),
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
            # A platform-injected PORT wins over CONSOLE_PORT (see below).
            "console_port": "CONSOLE_PORT",
        },
    )
    _optional(environ, raw, {"console_port": "PORT"})
    return _validate(Settings, raw)
