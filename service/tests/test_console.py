"""The operator console: pages, the auth gate, CSRF, secret hygiene, offline-only preview,
the connect bundle, and the worker's follow status."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from approved.config import load_settings
from approved.console.app import context_from_settings, create_app
from approved.console.auth import CSRF_COOKIE, SESSION_COOKIE
from approved.console.bundle import SHIM_SHA256, BundleError, build_bundle
from approved.state import DecisionRecord, JudgedEntry, StateStore

from .fakes import FACADE_URL, CallIdTracer, ScriptedReviewer, request_event, verdict

CONSOLE_TOKEN = "console-operator-token-7f3a9c1d2e4b"
SECRETS = {
    "CONSOLE_TOKEN": CONSOLE_TOKEN,
    "TENANT_TOKEN": "tenant-credential-a1b2c3d4e5f60718",
    "JUDGE_TG_BOT_TOKEN": "7002:judge-bot-secret-part-0011223344",
    "WANDB_API_KEY": "wandb-key-value-99887766554433",
    "INFERENCE_API_KEY": "inference-key-value-1234567890ab",
}
STATUS = {
    "running": True,
    "started_at": 1.0,
    "last_follow_at": None,
    "last_follow_ok": True,
    "last_error": None,
    "chain": "verified",
    "head_seq": 9,
    "caught_up": True,
    "pages": 3,
}


def _seed_state(state_dir: Path) -> None:
    store = StateStore(state_dir, "https://facade.test/a/demo")
    store.settle(
        "k-open",
        JudgedEntry(
            status="notified",
            seq=7,
            decision="NEEDS_HUMAN",
            call_id="call-open",
            trace_url="https://wandb.ai/bountify/judgy/r/call/call-open",
            action_class="vcs.push.main",
            summary="<script>alert('x')</script> git push origin main",
        ),
    )
    store.settle(
        "k-ready",
        JudgedEntry(status="notified", seq=3, decision="READY", action_class="vcs.push.branch"),
    )
    store.record_decision(
        "k-ready",
        DecisionRecord(event="approval.rejected", actor="human:demo", seq=5, ts="t"),
    )
    store.settle(
        "k-bad-trace",
        JudgedEntry(
            status="notified",
            seq=8,
            decision="READY",
            trace_url="javascript:alert(1)",
            action_class="fs.read",
        ),
    )
    store.save()


def _app(tmp_path: Path, *, token: bool = True, demo: bool = False, offline: bool = True):
    env = {
        "FACADE_URL": "https://facade.test/a/demo",
        "STATE_DIR": str(tmp_path),
        **{k: v for k, v in SECRETS.items() if k != "CONSOLE_TOKEN"},
        "REVIEWER_MODEL": "some-model",
    }
    if token:
        env["CONSOLE_TOKEN"] = CONSOLE_TOKEN
    if demo:
        env["APPROVED_DEMO"] = "1"
    if offline:
        env["OFFLINE"] = "1"
    settings = load_settings(env)
    return create_app(context_from_settings(settings, lambda: dict(STATUS)))


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    _seed_state(tmp_path)
    with TestClient(_app(tmp_path), base_url="https://console.test") as c:
        yield c


def _csrf(client: TestClient, path: str = "/policy") -> str:
    client.get(path)
    return client.cookies[CSRF_COOKIE]


def _login(client: TestClient) -> None:
    client.get("/login")
    response = client.post(
        "/login",
        data={"token": CONSOLE_TOKEN, "csrf": client.cookies[CSRF_COOKIE]},
        follow_redirects=False,
    )
    assert response.status_code == 303


# ------------------------------------------------------------------ pages


def test_public_pages_render(client: TestClient) -> None:
    policy = client.get("/policy")
    assert policy.status_code == 200
    assert "Policy builder" in policy.text
    assert "What the judge would say" in policy.text
    assert client.get("/health").json() == {
        "status": "ok",
        "worker": {"running": True, "chain": "verified"},
    }
    for asset in ("/static/console.css", "/static/console.js", "/static/policy.js"):
        assert client.get(asset).status_code == 200
    shim = client.get("/downloads/hermes-hook-shim.sh")
    assert shim.status_code == 200
    assert hashlib.sha256(shim.content).hexdigest() == SHIM_SHA256


def test_authenticated_pages_render(client: TestClient) -> None:
    _login(client)
    live = client.get("/")
    assert live.status_code == 200
    assert "Live tenant view" in live.text
    assert "vcs.push.main" in live.text
    assert 'href="https://wandb.ai/bountify/judgy/r/call/call-open"' in live.text
    assert "disagree" in live.text  # READY then rejected
    partial = client.get("/partials/live")
    assert partial.status_code == 200
    assert "Open requests" in partial.text
    assert client.get("/connect").status_code == 200
    metrics = client.get("/metrics").json()
    assert metrics["judge"]["false_ready"] == 1
    assert metrics["judge"]["disagree"] == 1
    assert metrics["open_requests"] == 2
    assert metrics["follow"]["head_seq"] == 9


def test_untrusted_text_is_escaped_and_bad_links_dropped(client: TestClient) -> None:
    _login(client)
    page = client.get("/").text
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in page
    assert "javascript:" not in page


def test_security_headers(client: TestClient) -> None:
    response = client.get("/policy")
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert "access-control-allow-origin" not in response.headers
    preflight = client.options(
        "/api/preview",
        headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers


# ------------------------------------------------------------------ auth gate


def test_auth_gate(client: TestClient) -> None:
    for path in ("/", "/connect"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    assert client.get("/partials/live").status_code == 401
    assert client.get("/metrics").status_code == 401
    assert client.post("/connect", data={}, follow_redirects=False).status_code == 303


def test_login_rejects_wrong_token_and_missing_csrf(client: TestClient) -> None:
    csrf = _csrf(client, "/login")
    bad = client.post("/login", data={"token": "nope", "csrf": csrf}, follow_redirects=False)
    assert bad.status_code == 401
    assert SESSION_COOKIE not in client.cookies
    no_csrf = client.post("/login", data={"token": CONSOLE_TOKEN}, follow_redirects=False)
    assert no_csrf.status_code == 403
    assert SESSION_COOKIE not in client.cookies


def test_session_cookie_is_not_the_token(client: TestClient) -> None:
    _login(client)
    cookie = client.cookies[SESSION_COOKIE]
    assert CONSOLE_TOKEN not in cookie
    raw = (
        client.post(
            "/login",
            data={"token": CONSOLE_TOKEN, "csrf": client.cookies[CSRF_COOKIE]},
            follow_redirects=False,
        )
        .headers["set-cookie"]
        .lower()
    )
    assert "httponly" in raw
    assert "samesite=strict" in raw


def test_forged_session_cookie_is_refused(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path), base_url="https://console.test") as c:
        c.cookies.set(SESSION_COOKIE, "0" * 64)
        assert c.get("/metrics").status_code == 401


def test_logout_needs_csrf_and_clears_the_session(client: TestClient) -> None:
    _login(client)
    assert client.post("/logout", data={}).status_code == 403
    response = client.post(
        "/logout", data={"csrf": client.cookies[CSRF_COOKIE]}, follow_redirects=False
    )
    assert response.status_code == 303
    assert client.get("/metrics").status_code == 401


def test_demo_mode_without_token_is_open_and_says_so(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, token=False, demo=True)) as c:
        page = c.get("/")
        assert page.status_code == 200
        assert "every page is open" in page.text
        assert c.get("/metrics").status_code == 200


def test_serve_refuses_without_token_outside_demo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, logs_captured
) -> None:
    from approved.__main__ import EXIT_CONFIG, main

    for name in ("CONSOLE_TOKEN", "CONSOLE_TOKEN_FILE", "APPROVED_DEMO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FACADE_URL", "https://facade.test")
    monkeypatch.setenv("TENANT_TOKEN", "t" * 24)
    monkeypatch.setenv("OFFLINE", "1")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    assert main(["serve"]) == EXIT_CONFIG
    assert "CONSOLE_TOKEN" in logs_captured.records("config.error")[0]["detail"]


# ------------------------------------------------------------------ secrets


def _every_response(c: TestClient) -> list[Any]:
    out = [c.get(p) for p in ("/policy", "/health", "/login", "/downloads/hermes-hook-shim.sh")]
    _login(c)
    csrf = c.cookies[CSRF_COOKIE]
    out += [c.get(p) for p in ("/", "/partials/live", "/connect", "/metrics")]
    for mode in ("maritime", "byo"):
        out.append(
            c.post(
                "/connect",
                data={
                    "tenant": "acme",
                    "facade_url": "https://api.maritime.sh/a/x",
                    "mode": mode,
                    "csrf": csrf,
                },
            )
        )
    out.append(c.post("/connect", data={"tenant": "BAD", "facade_url": "x", "csrf": csrf}))
    out.append(
        c.post(
            "/api/preview",
            json={"action_class": "vcs.push.main", "command": "git push"},
            headers={"x-csrf-token": csrf},
        )
    )
    out.append(c.post("/login", data={"token": "wrong", "csrf": csrf}))
    return out


@pytest.mark.parametrize("offline", [True, False])
def test_no_configured_secret_appears_in_any_response(tmp_path: Path, offline: bool) -> None:
    _seed_state(tmp_path)
    with TestClient(_app(tmp_path, offline=offline), base_url="https://console.test") as c:
        responses = _every_response(c)
    assert all(r.status_code < 500 for r in responses)
    for response in responses:
        blob = response.text + "\n" + "\n".join(f"{k}: {v}" for k, v in response.headers.items())
        for name, value in SECRETS.items():
            assert value not in blob, f"{name} leaked in {response.request.url}"
            assert value.split(":")[-1] not in blob


# ------------------------------------------------------------------ preview


def test_preview_runs_the_offline_reviewer_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import approved.reviewer as reviewer_module

    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the preview reached the live reviewer")

    monkeypatch.setattr(reviewer_module.LiveReviewer, "__init__", forbidden)
    monkeypatch.setattr(reviewer_module.LiveReviewer, "review", forbidden)
    with TestClient(
        _app(tmp_path, offline=False), base_url="https://console.test"
    ) as c:  # the worker would be LIVE
        csrf = _csrf(c)
        answer = c.post(
            "/api/preview",
            json={"action_class": "vcs.push.main", "command": "git push origin main"},
            headers={"x-csrf-token": csrf},
        )
        assert answer.status_code == 200
        body = answer.json()
        assert body["decision"] == "NEEDS_HUMAN"
        assert body["reviewer"] == "offline-rules-v1"
        ready = c.post(
            "/api/preview",
            json={"action_class": "vcs.push.branch", "command": "git push origin feat"},
            headers={"x-csrf-token": csrf},
        ).json()
        assert ready["decision"] == "READY"


def test_preview_requires_csrf_and_valid_input(client: TestClient) -> None:
    body = {"action_class": "vcs.push.main", "command": "git push"}
    assert client.post("/api/preview", json=body).status_code == 403
    csrf = _csrf(client)
    assert (
        client.post("/api/preview", json=body, headers={"x-csrf-token": "x" * 30}).status_code
        == 403
    )
    bad = {"action_class": "<b>", "command": "x"}
    assert client.post("/api/preview", json=bad, headers={"x-csrf-token": csrf}).status_code == 422
    huge = {"action_class": "a.b", "command": "x" * 5000}
    assert client.post("/api/preview", json=huge, headers={"x-csrf-token": csrf}).status_code == 422


# ------------------------------------------------------------------ connect bundle

TOKENISH = [
    re.compile(r"\b[0-9a-fA-F]{24,}\b"),
    re.compile(r"\b\d{3,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(sk|pk|mk|wandb|ghp|gho)[-_][A-Za-z0-9_-]{12,}"),
    re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"),
]


def _no_tokenish(text: str) -> None:
    for pattern in TOKENISH:
        assert pattern.search(text) is None, f"token-looking value: {pattern.pattern}"


def test_maritime_bundle_names_and_no_values() -> None:
    b = build_bundle(mode="maritime", tenant="acme-co", facade_url="https://api.maritime.sh/a/x1")
    s = b.script
    for name in (
        "APPROVAL_TENANT=acme-co",
        "APPROVAL_SERVE_AGENT_TOKEN=${agent}",
        "APPROVAL_SERVE_TENANT_TOKEN=${tenant_token}",
        "APPROVAL_SERVE_HOOK_HARNESS_CAP=300s",
        "APPROVAL_SERVE_HOOK_TIMEOUT=12s",
        "APPROVAL_TG_WEBHOOK_SECRET=${webhook_secret}",
        "APPROVAL_PUBLIC_URL=https://api.maritime.sh/a/x1",
        "APPROVAL_HUMAN=human:operator",
        "HOSTED_ACME_CO_TG_BOT_TOKEN=<",
        "HOSTED_ACME_CO_TG_CHAT=<",
        "APPROVAL_FACADE_URL_ENV=HOSTED_ACME_CO_FACADE_URL",
        "APPROVAL_FACADE_TOKEN_ENV=HOSTED_ACME_CO_FACADE_AGENT_TOKEN",
        "HOSTED_ACME_CO_FACADE_AGENT_TOKEN=${agent}",
        "maritime create acme-co-daemon --repo https://github.com/bountify-ai/approval-md-hosted"
        " --branch main --public --port 18789",
        'maritime env import acme-co-daemon "$dir/daemon.env"',
        "maritime create acme-co-hermes --repo https://github.com/bountify-ai/approval-hermes-image"
        " --branch main",
        'maritime env import acme-co-hermes "$dir/hermes.env"',
        "maritime stop acme-co-daemon && maritime start acme-co-daemon",
        "CONSOLE_TOKEN=${console_token}",
        "openssl rand -hex 24",
        "umask 077",
        'chmod 600 "$dir"/*.env',
    ):
        assert name in s, name
    judge = s[s.index('"$dir/judge.env"') :].split("EOF", 2)[1]
    assert "TENANT_TOKEN=${tenant_token}" in judge
    assert "${agent}" not in judge  # no agent credential
    assert "AGENT_TOKEN" not in judge
    assert "TG_BOT_TOKEN=<your approval bot" not in judge  # never the gate's bot
    assert "JUDGE_TG_BOT_TOKEN=<a SECOND bot" in judge
    _no_tokenish(s)
    assert b.hooks_yaml is None


def test_byo_bundle_hooks_and_shim() -> None:
    b = build_bundle(mode="byo", tenant="acme", facade_url="https://api.maritime.sh/a/x1")
    for name in (
        "APPROVAL_HOOK_URL_ENV=HOSTED_ACME_FACADE_URL",
        "APPROVAL_HOOK_TOKEN_ENV=HOSTED_ACME_FACADE_AGENT_TOKEN",
        "HOSTED_ACME_FACADE_AGENT_TOKEN=${agent}",
        "maritime create acme-daemon",
    ):
        assert name in b.script, name
    assert "acme-hermes" not in b.script
    assert b.hooks_yaml is not None
    assert "pre_tool_call:" in b.hooks_yaml
    assert "fail_closed: true" in b.hooks_yaml
    _no_tokenish(b.script)
    _no_tokenish(b.hooks_yaml)
    assert any(SHIM_SHA256 in n for n in b.notes)  # a checksum, stated as one


@pytest.mark.parametrize(
    ("tenant", "url"),
    [
        ("Acme", "https://x.test"),
        ("a", "https://x.test"),
        ("acme$(id)", "https://x.test"),
        ("acme", "http://x.test"),
        ("acme", "https://x.test/?q=1"),
        ("acme", 'https://x.test/"; rm -rf /'),
    ],
)
def test_bundle_refuses_unsafe_inputs(tenant: str, url: str) -> None:
    with pytest.raises(BundleError):
        build_bundle(mode="maritime", tenant=tenant, facade_url=url)


def test_connect_post_renders_escaped_bundle(client: TestClient) -> None:
    _login(client)
    csrf = client.cookies[CSRF_COOKIE]
    page = client.post(
        "/connect",
        data={
            "tenant": "acme",
            "facade_url": "https://api.maritime.sh/a/x",
            "mode": "byo",
            "csrf": csrf,
        },
    )
    assert page.status_code == 200
    assert "Bring your own Hermes" in page.text
    assert "HOSTED_ACME_FACADE_URL" in page.text
    assert "&lt;your approval bot token" in page.text
    refused = client.post(
        "/connect", data={"tenant": "x<y", "facade_url": "https://a.test", "csrf": csrf}
    )
    assert refused.status_code == 422
    assert "x&lt;y" in refused.text
    assert client.post("/connect", data={"tenant": "acme"}).status_code == 403


# ------------------------------------------------------------------ worker status


def test_worker_follow_status(make_worker, facade) -> None:
    facade.append(request_event("k1"))
    worker = make_worker(ScriptedReviewer([verdict()]), tracer=CallIdTracer())
    assert worker.status.snapshot()["chain"] == "unknown"
    worker.run(once=True)
    status = worker.status.snapshot()
    assert status["chain"] == "verified"
    assert status["head_seq"] == 1
    assert status["last_follow_ok"] is True
    assert status["running"] is False
    facade.records[0]["hash"] = "e" * 64
    facade.append(request_event("k2"))
    worker.run()
    assert worker.status.snapshot()["chain"] == "chain-break"
    assert FACADE_URL  # the fake facade the fixture used


# ------------------------------------------------------------------ N2: judge silence


def test_silence_is_recorded_and_shown(tmp_path: Path) -> None:
    store = StateStore(tmp_path, "https://facade.test/a/demo")
    store.settle("t", JudgedEntry(status="absent", seq=2, reason="timeout", action_class="x.y"))
    store.record_decision("t", DecisionRecord(event="approval.granted", actor="h", seq=3, ts="t"))
    store.record_decision(
        "never", DecisionRecord(event="approval.rejected", actor="h", seq=4, ts="t")
    )
    store.save()
    assert store.state.decisions["t"].advisory == "absent:timeout"
    assert store.state.decisions["never"].advisory == "absent:not-seen"
    assert store.state.judged["never"].reason == "not-seen"
    status = {**STATUS, "breaker": "open"}
    app = create_app(
        context_from_settings(
            load_settings(
                {
                    "FACADE_URL": "https://facade.test/a/demo",
                    "TENANT_TOKEN": "t" * 24,
                    "OFFLINE": "1",
                    "STATE_DIR": str(tmp_path),
                    "APPROVED_DEMO": "1",
                }
            ),
            lambda: status,
        )
    )
    with TestClient(app) as c:
        page = c.get("/").text
        metrics = c.get("/metrics").json()
    assert "judge silent: breaker open" in page
    assert "silent: timeout" in page
    assert "silent: not-seen" in page
    assert metrics["judge"]["silence"] == {"not-seen": 1, "timeout": 1}


# ------------------------------------------------------------------ S2: cookies and sessions


def _ctx_app(tmp_path: Path, auth: Any):
    from approved.console.app import ConsoleContext

    return create_app(
        ConsoleContext(
            auth=auth,
            state_dir=tmp_path,
            facade_url="https://facade.test",
            follow_status=lambda: dict(STATUS),
            mode="offline",
        )
    )


def test_cookie_attributes_on_a_shared_origin(tmp_path: Path) -> None:
    from pydantic import SecretStr

    from approved.console.auth import ConsoleAuth

    auth = ConsoleAuth(SecretStr(CONSOLE_TOKEN), demo=False, base_path="/a/agent-123")
    with TestClient(_ctx_app(tmp_path, auth), base_url="https://api.maritime.test") as c:
        page = c.get("/login")
        csrf_cookie = page.headers["set-cookie"].lower()
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert csrf is not None
        # The browser sends the path-scoped cookie to /a/agent-123/login; the platform's proxy
        # strips the prefix, so the app sees /login with the cookie.
        c.cookies.clear()
        login = c.post(
            "/login",
            data={"token": CONSOLE_TOKEN, "csrf": csrf.group(1)},
            headers={"cookie": f"{CSRF_COOKIE}={csrf.group(1)}"},
            follow_redirects=False,
        )
    raw = login.headers["set-cookie"].lower()
    for cookie in (raw, csrf_cookie):
        assert "secure" in cookie
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert "path=/a/agent-123" in cookie
    assert login.headers["location"] == "/a/agent-123/"
    assert 'href="/a/agent-123/static/console.css"' in page.text
    assert 'action="/a/agent-123/login"' in page.text
    assert '<meta name="base-path" content="/a/agent-123">' in page.text


def test_demo_cookies_are_not_secure(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, demo=True), base_url="http://127.0.0.1") as c:
        cookie = c.get("/login").headers.get("set-cookie", "").lower()
    assert "secure" not in cookie


def test_sessions_expire_after_12_hours_and_on_token_rotation(tmp_path: Path) -> None:
    from pydantic import SecretStr

    from approved.console.auth import ConsoleAuth

    now = [1_000_000.0]
    auth = ConsoleAuth(SecretStr(CONSOLE_TOKEN), demo=False, clock=lambda: now[0])
    with TestClient(_ctx_app(tmp_path, auth), base_url="https://console.test") as c:
        _login(c)
        assert c.get("/metrics").status_code == 200
        issued, mac = c.cookies[SESSION_COOKIE].split(".")
        assert issued == "1000000"
        assert CONSOLE_TOKEN not in mac
        now[0] += 12 * 3600 - 5
        assert c.get("/metrics").status_code == 200
        now[0] += 10
        assert c.get("/metrics").status_code == 401  # expired server-side
        c.cookies.set(SESSION_COOKIE, f"{int(now[0])}.{mac}")  # replayed MAC, new timestamp
        assert c.get("/metrics").status_code == 401
    rotated = ConsoleAuth(SecretStr("a-new-console-token-value"), demo=False, clock=lambda: now[0])
    with TestClient(_ctx_app(tmp_path, rotated), base_url="https://console.test") as c:
        c.cookies.set(SESSION_COOKIE, auth.session_cookie())
        assert c.get("/metrics").status_code == 401


# ------------------------------------------------------------------ S3: body limits, rate limit


def test_declared_oversize_bodies_are_refused_before_reading(client: TestClient) -> None:
    for path, limit in (("/login", 8 * 1024), ("/logout", 8 * 1024), ("/api/preview", 16 * 1024)):
        answer = client.post(path, content=b"x" * (limit + 1))
        assert answer.status_code == 413, path


def test_undeclared_oversize_bodies_are_cut_off_while_streaming(client: TestClient) -> None:
    def chunks():
        for _ in range(40):
            yield b"y" * 1024

    answer = client.post("/api/preview", content=chunks())  # chunked: no Content-Length
    assert answer.status_code == 413


def test_public_posts_are_rate_limited_per_client(tmp_path: Path) -> None:
    from approved.console.limits import RateLimiter

    settings = load_settings(
        {
            "FACADE_URL": "https://f.test",
            "TENANT_TOKEN": "t" * 24,
            "OFFLINE": "1",
            "STATE_DIR": str(tmp_path),
            "CONSOLE_TOKEN": CONSOLE_TOKEN,
        }
    )
    app = create_app(
        context_from_settings(settings, lambda: dict(STATUS)), rate_limiter=RateLimiter(limit=5)
    )
    with TestClient(app, base_url="https://console.test") as c:
        csrf = _csrf(c)
        codes = [
            c.post(
                "/api/preview",
                json={"action_class": "a.b", "command": "x"},
                headers={"x-csrf-token": csrf},
            ).status_code
            for _ in range(7)
        ]
    assert codes[:5] == [200] * 5
    assert codes[5:] == [429, 429]
