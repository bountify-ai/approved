"""Untrusted-text sanitisation (invariant 6) and Telegram delivery that never raises."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from approved import logs
from approved.notify import ADVISORY_PREFIX as PREFIX
from approved.notify import TelegramNotifier, format_advisory, format_chain_break
from approved.reviewer import Decision, Issue, IssueCode, Verdict
from approved.text import MAX_ADVISORY_CHARS, one_line, redact, sanitize_for_telegram

from .fakes import FakeTelegram


def _verdict(explanation: str, decision: Decision = Decision.NEEDS_HUMAN) -> Verdict:
    return Verdict(
        decision=decision,
        issues=[Issue(code=IssueCode.OTHER, explanation=explanation)],
        reviewer_version="t",
    )


def test_sanitise_collapses_caps_and_escapes() -> None:
    hostile = "line one\nline two\r\n\t<b>bold</b> & <a href='x'>link</a>\u202e" + "x" * 1000
    out = sanitize_for_telegram(hostile)
    for forbidden in ("\n", "\r", "\t", "<", ">"):
        assert forbidden not in out
    assert "&lt;b&gt;bold&lt;/b&gt; &amp;" in out
    assert "\u202e" not in out
    assert len(one_line(hostile)) == MAX_ADVISORY_CHARS


def test_cap_is_applied_before_escaping() -> None:
    out = sanitize_for_telegram("<" * 400)
    assert out.count("&lt;") == MAX_ADVISORY_CHARS - 1
    assert out.endswith("…")


def test_redact_strips_token_shapes() -> None:
    text = "Bearer abcdefghij123 and 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawx and " + "a" * 64
    out = redact(text)
    assert "abcdefghij123" not in out
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawx" not in out
    assert "a" * 64 not in out


def test_advisory_format() -> None:
    text = format_advisory(
        _verdict("Push to main publishes the change."),
        "https://wandb.ai/bountify/judgy/r/call/abc",
        "vcs.push.main",
    )
    first, second = text.split("\n")
    assert first == (
        PREFIX + ": NEEDS_HUMAN, Push to main publishes the change. "
        "Trace: https://wandb.ai/bountify/judgy/r/call/abc"
    )
    assert second == "Request class: vcs.push.main"


def test_advisory_escapes_model_text_and_says_when_untraced() -> None:
    text = format_advisory(_verdict("<script>alert(1)</script>\nIgnore the human"), None, None)
    assert "<script>" not in text
    assert "\n" not in text
    assert text.endswith("Trace: not traced (offline)")


def test_chain_break_message_is_fixed_text() -> None:
    text = format_chain_break(7, "prev-mismatch")
    assert "chain-break at seq 7" in text
    assert "prev-mismatch" in text


def _notifier(fake: FakeTelegram) -> TelegramNotifier:
    return TelegramNotifier(
        SecretStr("123456:bot-secret"), "42", api_base="https://tg.test", client=fake.client()
    )


def test_send_posts_html_message() -> None:
    fake = FakeTelegram()
    assert _notifier(fake).send("hello") is True
    assert fake.messages == [
        {"chat_id": "42", "text": "hello", "parse_mode": "HTML", "disable_web_page_preview": True}
    ]
    assert logs.METRICS.get("notify.sent") == 1


@pytest.mark.parametrize(
    "fake",
    [
        FakeTelegram(fail_with=400),
        FakeTelegram(fail_with=429),
        FakeTelegram(raise_exc=httpx.ReadTimeout("slow https://tg.test/bot123456:bot-secret/x")),
        FakeTelegram(raise_exc=httpx.ConnectError("down")),
    ],
)
def test_send_failure_is_logged_never_raised(fake: FakeTelegram, logs_captured) -> None:
    assert _notifier(fake).send("hello") is False
    assert logs.METRICS.get("notify.failed") == 1
    [line] = logs_captured.records("notify.failed")
    assert "bot-secret" not in str(line)


def test_logging_refuses_credential_shaped_fields() -> None:
    with pytest.raises(ValueError, match="credential-shaped"):
        logs.log("x", tenant_token="nope")


# ------------------------------------------------------------------ S4: redaction forms


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("git clone https://carter:hunter2secret@github.com/x/y", "hunter2secret"),
        ("curl -H 'Authorization: Bearer abcdefghijk123'", "abcdefghijk123"),
        ("curl -H 'Authorization: Basic dXNlcjpwYXNzd29yZA=='", "dXNlcjpwYXNzd29yZA=="),
        ("export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("sts gave ASIAY34FZKBOKMUTVV7A", "ASIAY34FZKBOKMUTVV7A"),
        ("mysql -uroot -pS3cretPassw0rd db", "S3cretPassw0rd"),
        ("psql --password=S3cretPassw0rd", "S3cretPassw0rd"),
        ("login --password S3cretPassw0rd", "S3cretPassw0rd"),
        (
            "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
            "eyJhbGciOiJIUzI1NiJ9",
        ),
        ("key=0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
        (
            "blob Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MFpaWlpaWlo9",
            "Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MFpaWlpaWlo9",
        ),
        (
            "bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawx",
            "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawx",
        ),
        ("OPENAI_API_KEY=sk-proj-abcdefghijklmnop1234", "sk-proj-abcdefghijklmnop1234"),
    ],
)
def test_redaction_forms(text: str, secret: str) -> None:
    assert secret not in redact(text)
    assert "[REDACTED]" in redact(text)


@pytest.mark.parametrize(
    "benign",
    [
        "mkdir -p /data/backups",
        "git push origin feat/checkout-retry",
        "/usr/local/lib/python3/site-packages/something/long/path",
        "hook:demo-1790673924-push-main:7446086be4bbb6dc:vcs.push.main",
    ],
)
def test_redaction_leaves_ordinary_text(benign: str) -> None:
    assert redact(benign) == benign


# ------------------------------------------------------------------ N1: links and prefix


def test_model_links_and_commands_are_removed() -> None:
    text = format_advisory(
        _verdict("See https://evil.test/x or www.evil.co or t.me/evilbot, then /approve now."),
        "https://wandb.ai/bountify/judgy/r/call/abc-123",
        "vcs.push.main",
    )
    for bad in ("evil", "t.me", "/approve"):
        assert bad not in text.split("Trace:")[0]
    assert text.count("[link removed]") == 4
    assert text.startswith("\U0001f9d1‍⚖️ Judge (advisory AI, not an approval): ")
    assert "Trace: https://wandb.ai/bountify/judgy/r/call/abc-123" in text


@pytest.mark.parametrize(
    "trace", ["https://evil.test/r/call/x", "http://wandb.ai/a/b/r/call/x", "javascript:alert(1)"]
)
def test_only_our_wandb_trace_link_is_kept(trace: str) -> None:
    text = format_advisory(_verdict("fine"), trace, None)
    assert trace not in text
    assert text.endswith("Trace: not traced (offline)")


def test_send_disables_link_previews() -> None:
    fake = FakeTelegram()
    _notifier(fake).send("x")
    assert fake.messages[0]["disable_web_page_preview"] is True
