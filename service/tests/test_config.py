"""Settings: file-first secrets, fail-closed requirements, no agent credential, no leaks."""

from __future__ import annotations

from pathlib import Path

import pytest

from approved.config import ConfigError, load_inference_settings, load_settings

BASE = {"FACADE_URL": "https://facade.test/a/t/", "TENANT_TOKEN": "inline-tenant-credential"}


def test_offline_minimum(tmp_path: Path) -> None:
    s = load_settings({**BASE, "OFFLINE": "1"})
    assert s.offline is True
    assert s.facade_url == "https://facade.test/a/t"
    assert s.judge_timeout_s == 60
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
        {**BASE, "OFFLINE": "1", "JUDGE_TG_BOT_TOKEN": "123:bot-value", "WANDB_API_KEY": "wk-value"}
    )
    text = repr(s) + str(s.model_dump())
    for secret in ("inline-tenant-credential", "bot-value", "wk-value"):
        assert secret not in text
    with pytest.raises(ConfigError) as info:
        load_settings({**BASE, "OFFLINE": "1", "FOLLOW_LIMIT": "inline-tenant-credential"})
    assert "inline-tenant-credential" not in str(info.value)


def _bundle_agent_names() -> list[str]:
    from approved.console.bundle import daemon_env, hermes_env, hook_env

    names = [v.name for v in hermes_env("acme-co", "https://f.test") if v.ref == "agent"]
    names += [v.name for v in hook_env("acme-co", "https://f.test") if v.ref == "agent"]
    names += [v.name for v in daemon_env("acme-co", "https://f.test", "op") if v.ref == "agent"]
    names += ["APPROVAL_FACADE_TOKEN_ENV", "HOSTED_ACME_CO_FACADE_AGENT_TOKEN_FILE"]
    return names


@pytest.mark.parametrize("name", _bundle_agent_names())
def test_bundle_agent_credential_names_are_refused(name: str) -> None:
    with pytest.raises(ConfigError, match="agent credential") as info:
        load_settings({**BASE, "OFFLINE": "1", name: "agent-secret-value-xyz"})
    assert "agent-secret-value-xyz" not in str(info.value)


@pytest.mark.parametrize(
    "name",
    [
        "HOSTED_ACME_TG_BOT_TOKEN",
        "HOSTED_ACME_CO_TG_BOT_TOKEN_FILE",
        "APPROVAL_TG_TOKEN",
        "TG_BOT_TOKEN",
        "TG_BOT_TOKEN_FILE",
    ],
)
def test_the_approval_bot_token_is_refused(name: str) -> None:
    with pytest.raises(ConfigError, match="approval bot token"):
        load_settings({**BASE, "OFFLINE": "1", name: "7001:gate-bot-secret"})


@pytest.mark.parametrize(
    ("url", "demo", "ok"),
    [
        ("https://api.maritime.sh/a/x", False, True),
        ("http://facade.example.com", False, False),
        ("http://10.0.0.5:8080", False, False),
        ("http://localhost:8088", False, True),
        ("http://127.0.0.1:8088", False, True),
        ("http://daemon:8080", False, False),
        ("http://facade.example.com", True, True),
        ("ftp://x", True, False),
    ],
)
def test_facade_url_must_be_https_off_loopback(url: str, demo: bool, ok: bool) -> None:
    env = {**BASE, "OFFLINE": "1", "FACADE_URL": url, **({"APPROVED_DEMO": "1"} if demo else {})}
    if ok:
        assert load_settings(env).facade_url == url.rstrip("/")
    else:
        with pytest.raises(ConfigError, match="https"):
            load_settings(env)


def test_a_single_label_http_facade_needs_an_explicit_opt_in() -> None:
    env = {**BASE, "OFFLINE": "1", "FACADE_URL": "http://daemon:8080"}
    with pytest.raises(ConfigError, match="ALLOW_INSECURE_FACADE"):
        load_settings(env)
    assert load_settings({**env, "ALLOW_INSECURE_FACADE": "1"}).facade_url == "http://daemon:8080"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("JUDGE_TIMEOUT_S", "inf"),
        ("JUDGE_TIMEOUT_S", "Infinity"),
        ("JUDGE_TIMEOUT_S", "nan"),
        ("JUDGE_TIMEOUT_S", "300.5"),
        ("JUDGE_TIMEOUT_S", "1e9"),
        ("HTTP_TIMEOUT_S", "inf"),
        ("HTTP_TIMEOUT_S", "121"),
        ("FEEDBACK_TIMEOUT_S", "inf"),
        ("FEEDBACK_TIMEOUT_S", "86400"),
    ],
)
def test_timeouts_are_bounded_and_the_refusal_names_the_variable(name: str, value: str) -> None:
    with pytest.raises(ConfigError) as refused:
        load_settings({**BASE, "OFFLINE": "1", name: value})
    assert name in str(refused.value)
    assert value not in str(refused.value)  # the message names variables, never values
    if name == "JUDGE_TIMEOUT_S":
        with pytest.raises(ConfigError, match=name):
            load_inference_settings({"OFFLINE": "1", name: value})


def test_timeout_bounds_are_inclusive() -> None:
    s = load_settings(
        {
            **BASE,
            "OFFLINE": "1",
            "JUDGE_TIMEOUT_S": "300",
            "HTTP_TIMEOUT_S": "120",
            "FEEDBACK_TIMEOUT_S": "120",
        }
    )
    assert (s.judge_timeout_s, s.http_timeout_s, s.feedback_timeout_s) == (300, 120, 120)
