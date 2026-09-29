"""Settings: file-first secrets, fail-closed requirements, no agent credential, no leaks."""

from __future__ import annotations

from pathlib import Path

import pytest

from approved.config import ConfigError, load_settings

BASE = {"FACADE_URL": "https://facade.test/a/t/", "TENANT_TOKEN": "inline-tenant-credential"}


def test_offline_minimum(tmp_path: Path) -> None:
    s = load_settings({**BASE, "OFFLINE": "1"})
    assert s.offline is True
    assert s.facade_url == "https://facade.test/a/t"
    assert s.judge_timeout_s == 25
    assert s.inference_base_url == "https://api.inference.wandb.ai/v1"
    assert s.weave_project == "bountify/judgy"
    assert s.telegram_enabled is False


def test_token_file_is_preferred(tmp_path: Path) -> None:
    f = tmp_path / "tenant"
    f.write_text("file-tenant-credential\n")
    s = load_settings({**BASE, "OFFLINE": "1", "TENANT_TOKEN_FILE": str(f)})
    assert s.tenant_token.get_secret_value() == "file-tenant-credential"


def test_unreadable_token_file_fails_without_value(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="TENANT_TOKEN_FILE"):
        load_settings({**BASE, "OFFLINE": "1", "TENANT_TOKEN_FILE": str(tmp_path / "missing")})


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"TENANT_TOKEN": "x"}, "FACADE_URL"),
        ({"FACADE_URL": "https://f.test"}, "TENANT_TOKEN"),
        ({**BASE, "FACADE_URL": "ftp://x"}, "http"),
        ({**BASE}, "REVIEWER_MODEL"),
        ({**BASE, "REVIEWER_MODEL": "m"}, "INFERENCE_API_KEY"),
        ({**BASE, "OFFLINE": "maybe"}, "OFFLINE"),
        ({**BASE, "OFFLINE": "1", "JUDGE_TIMEOUT_S": "-1"}, "judge_timeout_s"),
        ({**BASE, "OFFLINE": "1", "WANDB_PROJECT": "a/b/c"}, "WANDB_PROJECT"),
    ],
)
def test_missing_or_bad_settings_fail_closed(env: dict[str, str], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_settings(env)


def test_live_accepts_wandb_key_as_inference_key() -> None:
    s = load_settings({**BASE, "REVIEWER_MODEL": "m", "WANDB_API_KEY": "wandb-key-value"})
    assert s.inference_api_key is not None
    assert s.inference_api_key.get_secret_value() == "wandb-key-value"


def test_combined_project_spelling() -> None:
    s = load_settings({**BASE, "OFFLINE": "1", "WANDB_PROJECT": "acme/judge"})
    assert (s.wandb_entity, s.wandb_project) == ("acme", "judge")
    assert s.trace_url("abc") == "https://wandb.ai/acme/judge/r/call/abc"
    assert s.trace_url(None) is None


@pytest.mark.parametrize(
    "name", ["APPROVAL_SERVE_AGENT_TOKEN", "AGENT_TOKEN", "APPROVAL_SERVE_AGENT_TOKEN_FILE"]
)
def test_agent_credential_in_environment_refuses(name: str) -> None:
    with pytest.raises(ConfigError, match="agent credential") as info:
        load_settings({**BASE, "OFFLINE": "1", name: "agent-secret-value"})
    assert "agent-secret-value" not in str(info.value)


def test_secrets_never_in_repr_or_errors() -> None:
    s = load_settings(
        {**BASE, "OFFLINE": "1", "TG_BOT_TOKEN": "123:bot-value", "WANDB_API_KEY": "wk-value"}
    )
    text = repr(s) + str(s.model_dump())
    for secret in ("inline-tenant-credential", "bot-value", "wk-value"):
        assert secret not in text
    with pytest.raises(ConfigError) as info:
        load_settings({**BASE, "OFFLINE": "1", "FOLLOW_LIMIT": "inline-tenant-credential"})
    assert "inline-tenant-credential" not in str(info.value)
