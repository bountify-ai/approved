"""The console behind a proxy that drops the Cookie request header (Maritime's public proxy).

Every test here talks to the app through :class:`MaritimeProxy`, which does to each request
what Maritime's proxy was measured doing: it drops ``Cookie`` and ``Authorization``, strips the
``/a/<agent-id>`` prefix, and passes custom request headers and ``Set-Cookie`` responses. The
test client keeps and resends cookies as a browser would; none of them ever reaches the app.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from approved.config import load_settings
from approved.console.app import ConsoleContext, context_from_settings, create_app
from approved.console.auth import ConsoleAuth, SessionEpoch
from approved.console.bundle import SHIM_SHA256
from approved.console.limits import RateLimiter

from .test_console import CONSOLE_TOKEN, SECRETS, STATUS, _seed_state

PREFIX = "/a/agent-123"
ORIGIN = "https://api.maritime.test"
SESSION = "x-approved-session"
LOGIN = "x-approved-login"
LIVE_CONTENT = ("Live tenant view", "Open requests", "vcs.push.main", "Recent decisions")


class MaritimeProxy:
    """Drops Cookie and Authorization, strips the agent prefix, adds the forwarding hop. With
    ``drop=()`` it is a proxy that passes cookies (an operator's own domain under a prefix)."""

    def __init__(
        self,
        app: Any,
        prefix: str = PREFIX,
        drop: tuple[bytes, ...] = (b"cookie", b"authorization"),
    ) -> None:
        self.app = app
        self.prefix = prefix
        self.drop = drop
        self.dropped: list[bytes] = []
        self.urls: list[str] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        query = scope.get("query_string", b"").decode("latin-1")
        self.urls.append(path + ("?" + query if query else ""))
        if path != self.prefix and not path.startswith(self.prefix + "/"):
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"no such agent"})
            return
        kept = []
        for name, value in scope["headers"]:
            if name in self.drop:
                self.dropped.append(name)
            else:
                kept.append((name, value))
        inner = path[len(self.prefix) :] or "/"
        scope = {
            **scope,
            "path": inner,
            "raw_path": inner.encode(),
            "headers": [*kept, (b"x-forwarded-for", b"203.0.113.7")],
        }
        await self.app(scope, receive, send)


def _settings(tmp_path: Path, **extra: str):
    env = {
        "FACADE_URL": "https://facade.test/a/demo",
        "STATE_DIR": str(tmp_path),
        **SECRETS,
        "REVIEWER_MODEL": "some-model",
        "OFFLINE": "1",
        "PUBLIC_BASE_PATH": PREFIX,
        "TRUSTED_PROXY_HOPS": "1",
        **extra,
    }
    return load_settings(env)


def _maritime(tmp_path: Path, *, limiter: RateLimiter | None = None) -> MaritimeProxy:
    ctx = context_from_settings(_settings(tmp_path), lambda: dict(STATUS))
    return MaritimeProxy(create_app(ctx, rate_limiter=limiter))


@pytest.fixture
def proxy(tmp_path: Path) -> MaritimeProxy:
    _seed_state(tmp_path)
    return _maritime(tmp_path)


@pytest.fixture
def client(proxy: MaritimeProxy) -> Iterator[TestClient]:
    with TestClient(proxy, base_url=ORIGIN) as c:
        yield c


def _form_csrf(text: str) -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', text)
    assert found is not None
    return found.group(1)


def _meta(text: str, name: str) -> str:
    found = re.search(rf'<meta name="{name}" content="([^"]*)">', text)
    assert found is not None, name
    return found.group(1)


def _sign_in(c: TestClient) -> str:
    page = c.get(f"{PREFIX}/login")
    assert page.status_code == 200
    answer = c.post(
        f"{PREFIX}/login",
        data={"token": CONSOLE_TOKEN, "csrf": _form_csrf(page.text)},
        headers={LOGIN: "1"},
        follow_redirects=False,
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["flow"] == "header"
    assert "set-cookie" not in answer.headers
    return body["session"]


# ------------------------------------------------------------------ the defect, and the fix


def test_the_form_sign_in_fails_behind_the_proxy(client: TestClient, proxy: MaritimeProxy) -> None:
    """The measured defect: the CSRF cookie is set, the proxy drops it, the form gets 403."""
    page = client.get(f"{PREFIX}/login")
    assert "approved_csrf=" in page.headers["set-cookie"]
    answer = client.post(
        f"{PREFIX}/login",
        data={"token": CONSOLE_TOKEN, "csrf": _form_csrf(page.text)},
        follow_redirects=False,
    )
    assert answer.status_code == 403
    assert "Session expired; try again." in answer.text
    assert b"cookie" in proxy.dropped  # the client did send it; the proxy dropped it


def test_every_page_and_post_works_without_the_cookie_header(
    client: TestClient, proxy: MaritimeProxy
) -> None:
    session = _sign_in(client)
    h = {SESSION: session}

    live = client.get(f"{PREFIX}/", headers=h)
    assert live.status_code == 200
    for text in LIVE_CONTENT:
        assert text in live.text
    assert _meta(live.text, "session-flow") == "header"
    assert _meta(live.text, "base-path") == PREFIX
    assert f'<script src="{PREFIX}/static/session.js"></script>' in live.text
    assert "set-cookie" not in live.headers  # nothing to set: the header carries the session
    csrf = _meta(live.text, "csrf-token")
    assert f'name="csrf" value="{csrf}"' in live.text  # the sign-out form carries the same one

    partial = client.get(f"{PREFIX}/partials/live", headers=h)
    assert partial.status_code == 200
    assert "Open requests" in partial.text

    metrics = client.get(f"{PREFIX}/metrics", headers=h)
    assert metrics.status_code == 200
    assert metrics.json()["open_requests"] == 2

    policy = client.get(f"{PREFIX}/policy", headers=h)
    assert policy.status_code == 200
    assert "Sign out" in policy.text  # the signed-in variant
    assert _meta(policy.text, "csrf-token") == csrf

    preview = client.post(
        f"{PREFIX}/api/preview",
        json={"action_class": "vcs.push.main", "command": "git push origin main"},
        headers={**h, "x-csrf-token": csrf},
    )
    assert preview.status_code == 200
    assert preview.json()["decision"] == "NEEDS_HUMAN"

    connect = client.get(f"{PREFIX}/connect", headers=h)
    assert connect.status_code == 200
    assert "Connect a tenant" in connect.text
    built = client.post(
        f"{PREFIX}/connect",
        data={
            "tenant": "acme",
            "facade_url": "https://api.maritime.sh/a/x",
            "mode": "byo",
            "csrf": csrf,
        },
        headers=h,
    )
    assert built.status_code == 200
    assert "HOSTED_ACME_FACADE_URL" in built.text
    assert f'href="{PREFIX}/downloads/hermes-hook-shim.sh"' in built.text
    refused = client.post(
        f"{PREFIX}/connect",
        data={"tenant": "x<y", "facade_url": "https://a.test", "csrf": csrf},
        headers=h,
    )
    assert refused.status_code == 422
    assert "x&lt;y" in refused.text

    shim = client.get(f"{PREFIX}/downloads/hermes-hook-shim.sh")  # public: no session needed
    assert shim.status_code == 200
    assert hashlib.sha256(shim.content).hexdigest() == SHIM_SHA256
    assert client.get(f"{PREFIX}/static/session.js").status_code == 200

    out = client.post(f"{PREFIX}/logout", data={"csrf": csrf}, headers=h)
    assert out.status_code == 200
    assert out.json() == {"detail": "signed out"}
    for path in ("/", "/metrics", "/partials/live", "/connect"):
        assert client.get(f"{PREFIX}{path}", headers=h).status_code == 401, path
    assert b"cookie" in proxy.dropped  # every one of those ran with the Cookie header dropped


BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


def test_header_flow_answers_are_not_cached(client: TestClient) -> None:
    page = client.get(f"{PREFIX}/login")
    login = client.post(
        f"{PREFIX}/login",
        data={"token": CONSOLE_TOKEN, "csrf": _form_csrf(page.text)},
        headers={LOGIN: "1"},
    )
    assert login.json()["flow"] == "header"
    answers = [login]
    h = {SESSION: login.json()["session"]}
    answers += [
        client.get(f"{PREFIX}{p}", headers=h)
        for p in ("/", "/policy", "/connect", "/partials/live", "/metrics")
    ]
    csrf = _meta(answers[1].text, "csrf-token")
    answers.append(
        client.post(
            f"{PREFIX}/connect",
            data={"tenant": "acme", "facade_url": "https://api.maritime.sh/a/x", "csrf": csrf},
            headers=h,
        )
    )
    answers.append(client.get(f"{PREFIX}/metrics", headers={SESSION: "0.forged"}))
    answers.append(client.post(f"{PREFIX}/logout", data={"csrf": csrf}, headers=h))
    for answer in answers:
        assert answer.status_code in (200, 401), answer.request.url
        assert answer.headers["cache-control"] == "no-store", answer.request.url


def test_a_browser_navigation_to_metrics_goes_to_sign_in(client: TestClient) -> None:
    """A reload of /metrics in a header-mode tab carries no header: it must reach the sign-in
    document (whose script restores the page), not strand the tab on raw 401 JSON."""
    nav = client.get(
        f"{PREFIX}/metrics", headers={"accept": BROWSER_ACCEPT}, follow_redirects=False
    )
    assert nav.status_code == 303
    assert nav.headers["location"] == f"{PREFIX}/login"
    for accept in ("*/*", "application/json", ""):
        api = client.get(f"{PREFIX}/metrics", headers={"accept": accept}, follow_redirects=False)
        assert api.status_code == 401, accept
        assert api.json() == {"detail": "sign in at /login"}
    session = _sign_in(client)
    signed = client.get(f"{PREFIX}/metrics", headers={SESSION: session, "accept": BROWSER_ACCEPT})
    assert signed.status_code == 200
    assert signed.json()["open_requests"] == 2
    dead = client.get(f"{PREFIX}/metrics", headers={SESSION: "0.x", "accept": BROWSER_ACCEPT})
    assert dead.status_code == 401  # a script's request with a dead header: 401, not a redirect


# ------------------------------------------------------------------ nothing without a session


def test_unauthenticated_requests_get_no_page_content(client: TestClient) -> None:
    for path in ("/", "/connect"):
        answer = client.get(f"{PREFIX}{path}", follow_redirects=False)
        assert answer.status_code == 303
        assert answer.headers["location"] == f"{PREFIX}/login"
    login = client.get(f"{PREFIX}/login")
    assert _meta(login.text, "session-flow") == "none"
    forged = {SESSION: "1790683230." + "0" * 64}
    for garbage in ("", "x", "1.2", "9" * 200, forged[SESSION]):
        for path in ("/", "/connect", "/partials/live", "/metrics", "/policy"):
            answer = client.get(f"{PREFIX}{path}", headers={SESSION: garbage})
            assert answer.status_code == 401, (garbage, path)
            assert answer.json() == {"detail": "session expired; sign in again"}
            for text in (*LIVE_CONTENT, "Policy builder", "Connect a tenant"):
                assert text not in answer.text
    connect = {"tenant": "acme", "facade_url": "https://a.test", "csrf": "x"}
    assert client.post(f"{PREFIX}/connect", data=connect, headers=forged).status_code == 401
    assert client.post(f"{PREFIX}/logout", data={"csrf": "x"}, headers=forged).status_code == 401
    preview = {"action_class": "a.b", "command": "x"}
    assert client.post(f"{PREFIX}/api/preview", json=preview, headers=forged).status_code == 401
    # The public routes stay reachable with a dead header, so the sign-in can recover.
    assert client.get(f"{PREFIX}/health", headers=forged).status_code == 200
    assert client.get(f"{PREFIX}/login", headers=forged).status_code == 200
    assert client.get(f"{PREFIX}/static/console.js", headers=forged).status_code == 200


def _own_domain(tmp_path: Path) -> MaritimeProxy:
    ctx = context_from_settings(_settings(tmp_path), lambda: dict(STATUS))
    return MaritimeProxy(create_app(ctx), drop=())


def test_a_dead_header_never_falls_back_to_a_valid_cookie(tmp_path: Path) -> None:
    """Where cookies arrive: a valid cookie session plus a forged header is judged by the
    header."""
    _seed_state(tmp_path)
    with TestClient(_own_domain(tmp_path), base_url=ORIGIN) as c:
        page = c.get(f"{PREFIX}/login")
        c.post(
            f"{PREFIX}/login",
            data={"token": CONSOLE_TOKEN, "csrf": _form_csrf(page.text)},
            follow_redirects=False,
        )
        assert c.get(f"{PREFIX}/metrics").status_code == 200
        assert c.get(f"{PREFIX}/metrics", headers={SESSION: "0.forged"}).status_code == 401


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


def test_auth_judges_a_header_request_by_the_header_alone() -> None:
    """Unit level, below the gate middleware: a forged header beside a valid cookie is not
    signed in, and its CSRF check never consults the cookie."""
    auth = ConsoleAuth(SecretStr(CONSOLE_TOKEN), demo=False)
    good = auth.session_cookie()
    cookie = {"cookie": f"approved_console={good}; approved_csrf={'c' * 30}"}
    assert auth.is_authenticated(_request(cookie))
    assert auth.flow(_request(cookie)) == "cookie"
    forged = _request({**cookie, SESSION: "0.forged"})
    assert not auth.is_authenticated(forged)
    assert auth.flow(forged) == "none"
    assert not auth.csrf_ok(forged, "c" * 30)  # the cookie's token is not the header's
    valid = _request({**cookie, SESSION: good})
    assert auth.flow(valid) == "header"
    assert not auth.csrf_ok(valid, "c" * 30)
    assert auth.csrf_ok(valid, auth.session_csrf(good))
    assert auth.csrf_for(valid) == (auth.session_csrf(good), False)
    assert not auth.verify_session(good + "0" * 128)  # over-long values are refused unparsed


def test_a_session_value_has_exactly_one_accepted_spelling() -> None:
    now = [1_000_000.0]
    auth = ConsoleAuth(SecretStr(CONSOLE_TOKEN), demo=False, clock=lambda: now[0])
    good = auth.session_cookie()
    issued, mac = good.split(".")
    assert auth.verify_session(good)
    for bad in (
        f"0{issued}.{mac}",  # leading zero
        f"+{issued}.{mac}",  # sign
        f" {issued}.{mac}",
        f"{issued}.{mac}\n",
        f"{issued}.{mac.upper()}",
        f"{issued}.{mac}0",
        f"{issued}.{mac[:-1]}",
        f"{issued}..{mac}",
        "\u00b2.{mac}".replace("{mac}", mac),  # superscript two: isdigit() said yes
        "\u00b9\u00b2\u00b3." + mac,
        "".join(chr(0x0660 + int(d)) for d in issued) + "." + mac,  # Arabic-Indic digits
        "".join(chr(0xFF10 + int(d)) for d in issued) + "." + mac,  # fullwidth digits
        "1" * 13 + "." + mac,
        "",
        ".",
    ):
        assert not auth.verify_session(bad), repr(bad)


def _raw_get(app: Any, path: str, headers: list[tuple[bytes, bytes]]) -> int:
    """One GET straight into the ASGI app with these exact header bytes. The test client
    re-encodes non-ASCII header bytes as UTF-8 (``\\xb2`` arrives as ``\\xc2\\xb2``), so it
    cannot deliver what a raw HTTP client can."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"console.test"), *headers],
        "client": ("203.0.113.7", 1234),
        "server": ("console.test", 443),
    }
    status: list[int] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    anyio.run(app, scope, receive, send)
    return status[0]


RAW_BAD = (
    b"\xb2." + b"a" * 64,  # superscript two in latin-1: str.isdigit() said yes, int() raised
    b"\xb9\xb2\xb3." + b"a" * 64,
    b"01000000." + b"0" * 64,
    b"+1." + b"0" * 64,
)


@pytest.mark.parametrize("raw", RAW_BAD)
def test_malformed_session_bytes_are_401_never_500(tmp_path: Path, raw: bytes) -> None:
    app = create_app(context_from_settings(_settings(tmp_path), lambda: dict(STATUS)))
    for path in ("/", "/metrics", "/partials/live", "/connect", "/policy"):
        assert _raw_get(app, path, [(SESSION.encode(), raw)]) == 401, (raw, path)
    for path in ("/login", "/health"):  # exempt routes still answer, never 500
        assert _raw_get(app, path, [(SESSION.encode(), raw)]) == 200, (raw, path)
    cookie = [(b"cookie", b"approved_console=" + raw)]
    assert _raw_get(app, "/metrics", cookie) == 401
    assert _raw_get(app, "/", cookie) == 303


def _clocked(tmp_path: Path, now: list[float], token: str = CONSOLE_TOKEN) -> MaritimeProxy:
    auth = ConsoleAuth(
        SecretStr(token),
        demo=False,
        base_path=PREFIX,
        clock=lambda: now[0],
        epoch=SessionEpoch(tmp_path / "console-epoch"),
    )
    ctx = ConsoleContext(
        auth=auth,
        state_dir=tmp_path,
        facade_url="https://facade.test",
        follow_status=lambda: dict(STATUS),
        mode="offline",
        trusted_proxy_hops=1,
    )
    return MaritimeProxy(create_app(ctx))


def test_expired_replayed_revoked_and_rotated_header_sessions_are_refused(
    tmp_path: Path,
) -> None:
    now = [1_000_000.0]
    with TestClient(_clocked(tmp_path, now), base_url=ORIGIN) as c:
        session = _sign_in(c)
        issued, mac = session.split(".")
        assert issued == "1000000"
        assert CONSOLE_TOKEN not in session
        assert c.get(f"{PREFIX}/metrics", headers={SESSION: session}).status_code == 200
        now[0] += 12 * 3600 - 5
        assert c.get(f"{PREFIX}/metrics", headers={SESSION: session}).status_code == 200
        now[0] += 10
        assert c.get(f"{PREFIX}/metrics", headers={SESSION: session}).status_code == 401
        replayed = f"{int(now[0])}.{mac}"  # the old MAC under a fresh timestamp
        assert c.get(f"{PREFIX}/metrics", headers={SESSION: replayed}).status_code == 401

        fresh = _sign_in(c)
        other = _sign_in(c)
        live = c.get(f"{PREFIX}/", headers={SESSION: fresh})
        c.post(
            f"{PREFIX}/logout",
            data={"csrf": _meta(live.text, "csrf-token")},
            headers={SESSION: fresh},
        )
        for kept in (fresh, other):  # sign-out revokes every session, as in the cookie flow
            assert c.get(f"{PREFIX}/metrics", headers={SESSION: kept}).status_code == 401
        survivor = _sign_in(c)
    with TestClient(_clocked(tmp_path, now, token="a-rotated-console-token"), base_url=ORIGIN) as c:
        assert c.get(f"{PREFIX}/metrics", headers={SESSION: survivor}).status_code == 401


# ------------------------------------------------------------------ CSRF in the header flow


def test_state_changing_requests_need_the_header_and_this_sessions_csrf(tmp_path: Path) -> None:
    # A session value is <issued_at>.<MAC>: two sign-ins in the same second get the same value
    # (and so the same CSRF token). A second apart they are distinct sessions.
    now = [1_000_000.0]
    with TestClient(_clocked(tmp_path, now), base_url=ORIGIN) as client:
        a = _sign_in(client)
        now[0] += 1
        b = _sign_in(client)
        csrf_a = _meta(client.get(f"{PREFIX}/", headers={SESSION: a}).text, "csrf-token")
        csrf_b = _meta(client.get(f"{PREFIX}/", headers={SESSION: b}).text, "csrf-token")
        assert csrf_a != csrf_b
        assert a not in csrf_a  # the token is a MAC over the session, not the session
        cookie_style = _form_csrf(client.get(f"{PREFIX}/login").text)
        form = {"tenant": "acme", "facade_url": "https://api.maritime.sh/a/x", "mode": "maritime"}
        preview = {"action_class": "a.b", "command": "x"}

        for wrong in (csrf_b, cookie_style, "", None):
            data = {**form, "csrf": wrong} if wrong is not None else form
            assert (
                client.post(f"{PREFIX}/connect", data=data, headers={SESSION: a}).status_code == 403
            )
            out = {"csrf": wrong} if wrong is not None else {}
            assert (
                client.post(f"{PREFIX}/logout", data=out, headers={SESSION: a}).status_code == 403
            )
            pre = {"x-csrf-token": wrong} if wrong else {}
            answer = client.post(f"{PREFIX}/api/preview", json=preview, headers={SESSION: a, **pre})
            assert answer.status_code == 403
        assert (
            client.get(f"{PREFIX}/metrics", headers={SESSION: a}).status_code == 200
        )  # not revoked

        # The right token but no session header: the request is a cookie-flow request, and the
        # proxy dropped the cookies. Nothing happens.
        connect = client.post(
            f"{PREFIX}/connect", data={**form, "csrf": csrf_a}, follow_redirects=False
        )
        assert connect.status_code == 303
        assert connect.headers["location"] == f"{PREFIX}/login"
        assert client.post(f"{PREFIX}/logout", data={"csrf": csrf_a}).status_code == 403
        assert (
            client.post(f"{PREFIX}/api/preview", json=preview, headers={"x-csrf-token": csrf_a})
        ).status_code == 403
        assert client.get(f"{PREFIX}/metrics", headers={SESSION: a}).status_code == 200

        ok = client.post(f"{PREFIX}/connect", data={**form, "csrf": csrf_a}, headers={SESSION: a})
        assert ok.status_code == 200


def test_the_header_sign_in_needs_the_login_header(client: TestClient) -> None:
    """Without ``X-Approved-Login`` a sign-in is the cookie flow's form post: no JSON, no
    session value, and behind the proxy a 403."""
    page = client.get(f"{PREFIX}/login")
    answer = client.post(
        f"{PREFIX}/login",
        data={"token": CONSOLE_TOKEN, "csrf": _form_csrf(page.text)},
        headers={"accept": "application/json"},
        follow_redirects=False,
    )
    assert answer.status_code == 403
    assert answer.headers["content-type"].startswith("text/html")
    assert '"flow"' not in answer.text
    assert '"session"' not in answer.text


def test_header_sign_ins_are_rate_limited_like_the_form(tmp_path: Path) -> None:
    proxy = _maritime(tmp_path, limiter=RateLimiter(limit=3))
    with TestClient(proxy, base_url=ORIGIN) as c:
        codes = [
            c.post(f"{PREFIX}/login", data={"token": "wrong"}, headers={LOGIN: "1"}).status_code
            for _ in range(5)
        ]
        assert codes == [401, 401, 401, 429, 429]
        wrong = c.post(f"{PREFIX}/login", data={"token": "wrong"}, headers={LOGIN: "1"})
        assert wrong.json() == {"detail": "Too many failed attempts."}
        assert _sign_in(c)  # the valid token is never limited
        oversize = c.post(f"{PREFIX}/login", content=b"token=" + b"x" * 9000, headers={LOGIN: "1"})
        assert oversize.status_code == 413


def test_the_scripted_sign_in_keeps_the_cookie_flow_where_cookies_arrive(tmp_path: Path) -> None:
    """Own domain or the local demo: the same script-made sign-in gets the cookie, exactly as
    the form does, and no session value in the body."""
    with TestClient(_own_domain(tmp_path), base_url=ORIGIN) as c:
        page = c.get(f"{PREFIX}/login")
        answer = c.post(
            f"{PREFIX}/login",
            data={"token": CONSOLE_TOKEN, "csrf": _form_csrf(page.text)},
            headers={LOGIN: "1"},
        )
        assert answer.status_code == 200
        assert answer.json() == {"flow": "cookie"}
        raw = answer.headers["set-cookie"].lower()
        for attribute in ("httponly", "samesite=strict", "secure", f"path={PREFIX}"):
            assert attribute in raw
        live = c.get(f"{PREFIX}/")
        assert live.status_code == 200
        assert _meta(live.text, "session-flow") == "cookie"
        assert "session" not in answer.text
        # A stale CSRF cookie still fails the double-submit, header or not.
        c.cookies.clear()
        c.cookies.set("approved_csrf", "y" * 30)
        stale = c.post(
            f"{PREFIX}/login", data={"token": CONSOLE_TOKEN, "csrf": "z" * 30}, headers={LOGIN: "1"}
        )
        assert stale.status_code == 403
        assert stale.json() == {"detail": "Session expired; try again."}


def test_demo_mode_without_a_token_ignores_the_header(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "FACADE_URL": "https://facade.test/a/demo",
            "TENANT_TOKEN": "t" * 24,
            "OFFLINE": "1",
            "STATE_DIR": str(tmp_path),
            "APPROVED_DEMO": "1",
        }
    )
    app = create_app(context_from_settings(settings, lambda: dict(STATUS)))
    with TestClient(app) as c:
        page = c.get("/", headers={SESSION: "anything"})
        assert page.status_code == 200
        assert _meta(page.text, "session-flow") == "open"


def test_console_off_serves_only_health_whatever_the_header(tmp_path: Path) -> None:
    settings = _settings(tmp_path, CONSOLE_ENABLED="0")
    app = create_app(context_from_settings(settings, lambda: dict(STATUS)), console=False)
    with TestClient(MaritimeProxy(app), base_url=ORIGIN) as c:
        assert c.get(f"{PREFIX}/health", headers={SESSION: "x"}).status_code == 200
        for path in ("/", "/login", "/metrics", "/static/session.js"):
            assert c.get(f"{PREFIX}{path}", headers={SESSION: "x"}).status_code == 404


# ------------------------------------------------------------------ CORS


ROUTES = ("/", "/login", "/logout", "/policy", "/connect", "/partials/live", "/metrics")


def test_no_cors_allow_header_on_any_response(client: TestClient) -> None:
    """A cross-origin page can send the custom headers only after a preflight this app never
    answers, so ``X-Approved-Session`` and ``X-Approved-Login`` are same-origin only."""
    evil = {"origin": "https://evil.test"}
    responses = []
    for path in (*ROUTES, "/api/preview", "/health"):
        responses.append(
            client.options(
                f"{PREFIX}{path}",
                headers={
                    **evil,
                    "access-control-request-method": "POST",
                    "access-control-request-headers": "x-approved-session, x-approved-login",
                },
            )
        )
    session = _sign_in(client)
    for path in ROUTES:
        responses.append(client.get(f"{PREFIX}{path}", headers={**evil, SESSION: session}))
    responses.append(
        client.post(f"{PREFIX}/login", data={"token": CONSOLE_TOKEN}, headers={**evil, LOGIN: "1"})
    )
    for response in responses:
        leaked = [k for k in response.headers if k.lower().startswith("access-control-")]
        assert leaked == [], (response.request.url, leaked)


# ------------------------------------------------------------------ secrets


def test_no_secret_or_session_value_in_urls_logs_or_other_responses(
    tmp_path: Path, logs_captured
) -> None:
    _seed_state(tmp_path)
    proxy = _maritime(tmp_path)
    responses: list[Any] = []
    with TestClient(proxy, base_url=ORIGIN) as c:
        page = c.get(f"{PREFIX}/login")
        responses.append(page)
        login = c.post(
            f"{PREFIX}/login",
            data={"token": CONSOLE_TOKEN, "csrf": _form_csrf(page.text)},
            headers={LOGIN: "1"},
        )
        session = login.json()["session"]
        h = {SESSION: session}
        responses += [c.get(f"{PREFIX}{p}", headers=h) for p in ROUTES if p != "/logout"]
        responses += [c.get(f"{PREFIX}{p}") for p in ("/", "/policy", "/health")]
        csrf = _meta(responses[1].text, "csrf-token")
        for mode in ("maritime", "byo"):
            responses.append(
                c.post(
                    f"{PREFIX}/connect",
                    data={
                        "tenant": "acme",
                        "facade_url": "https://api.maritime.sh/a/x",
                        "mode": mode,
                        "csrf": csrf,
                    },
                    headers=h,
                )
            )
        responses.append(
            c.post(
                f"{PREFIX}/api/preview",
                json={"action_class": "vcs.push.main", "command": "git push"},
                headers={**h, "x-csrf-token": csrf},
            )
        )
        responses.append(c.post(f"{PREFIX}/login", data={"token": "wrong"}, headers={LOGIN: "1"}))
        responses.append(c.post(f"{PREFIX}/logout", data={"csrf": csrf}, headers=h))
        responses.append(c.get(f"{PREFIX}/metrics", headers=h))  # 401 after sign-out
    assert all(r.status_code < 500 for r in responses)
    for response in [login, *responses]:
        blob = response.text + "\n" + "\n".join(f"{k}: {v}" for k, v in response.headers.items())
        for name, value in SECRETS.items():
            assert value not in blob, f"{name} leaked in {response.request.url}"
    mac = session.split(".")[1]
    for response in responses:  # every response except the sign-in answer itself
        blob = response.text + "\n" + "\n".join(f"{k}: {v}" for k, v in response.headers.items())
        assert mac not in blob, f"session value in {response.request.url}"
    for url in proxy.urls:
        assert mac not in url
        assert CONSOLE_TOKEN not in url
    logged = logs_captured.stream.getvalue()
    assert mac not in logged
    assert CONSOLE_TOKEN not in logged


# ------------------------------------------------------------------ the browser script


def test_session_script_under_node() -> None:
    """Runs ``static/session.js`` and ``static/console.js`` against a stub DOM in Node: the
    sign-in, the swap, the header on every request, the fragment-only navigation, sign-out and
    expiry. A unit-level harness, not a browser: see ``tests/js/session_harness.js``."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    here = Path(__file__).parent
    static = here.parent / "src" / "approved" / "console" / "static"
    result = subprocess.run(  # noqa: S603 - fixed argv, repository files only
        [node, str(here / "js" / "session_harness.js"), str(static)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "session harness: all" in result.stdout
