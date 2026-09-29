"""The console web app (FastAPI). Served by ``python -m approved serve`` beside the worker.

Routes:

====================  ======  ==================================================
path                  auth    what
====================  ======  ==================================================
``/health``           public  liveness for the platform, plus worker/chain state
``/policy``           public  the policy builder and the offline judge preview
``/api/preview``      public  offline reviewer only (CSRF header required)
``/downloads/...``    public  the vendored Hermes hook shim (no secrets in it)
``/login``            public  sign in with the operator token
``/``                 token   the live tenant view
``/partials/live``    token   the live view's fragment, polled by the page
``/connect``          token   the connect bundle
``/metrics``          token   JSON counters
====================  ======  ==================================================

It reads the judge's state file and the worker's in-process follow status only: no page
view calls the facade. No CORS middleware is installed, so browsers apply same-origin.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from ..config import Settings
from ..logs import METRICS
from ..reviewer import JudgeRequest, OfflineReviewer
from .auth import CSRF_COOKIE, SESSION_COOKIE, SESSION_MAX_AGE_S, ConsoleAuth
from .bundle import BundleError, build_bundle
from .limits import RateLimiter, RequestGuard
from .live import load_view
from .pages import connect_page, live_fragment, live_page, login_page, policy_page

__all__ = ["ConsoleContext", "context_from_settings", "create_app"]

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}
MAX_FORM_BYTES = 8 * 1024


@dataclass(frozen=True)
class ConsoleContext:
    auth: ConsoleAuth
    state_dir: Path
    facade_url: str
    follow_status: Callable[[], dict[str, Any]]
    mode: str  # "offline" | "live"

    @property
    def facade_host(self) -> str:
        return urlsplit(self.facade_url).netloc or self.facade_url


def context_from_settings(
    settings: Settings, follow_status: Callable[[], dict[str, Any]]
) -> ConsoleContext:
    """The console's view of the settings: the console token and non-secret fields only."""
    return ConsoleContext(
        auth=ConsoleAuth(
            settings.console_token,
            demo=settings.demo_mode,
            base_path=settings.public_base_path,
        ),
        state_dir=settings.state_dir,
        facade_url=settings.facade_url,
        follow_status=follow_status,
        mode="offline" if settings.offline else "live",
    )


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_class: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.*:-]+$")
    command: str = Field(default="", max_length=2000)


class _SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


_OWN_LINK = re.compile(r'((?:href|src|action)=")/(?!/)')


def rebase(html: str, base: str) -> str:
    """Prefix this console's own absolute links with its public base path (``/a/<id>`` on a
    shared Maritime origin). External links (``https://...``) are untouched."""
    if not base:
        return html
    html = html.replace(
        '<meta name="base-path" content="">', f'<meta name="base-path" content="{base}">'
    )
    return _OWN_LINK.sub(lambda m: m.group(1) + base + "/", html)


def _demo_note(ctx: ConsoleContext) -> str | None:
    if not ctx.auth.demo:
        return None
    if ctx.auth.enabled:
        return "Demo mode: local stack, fake Telegram, throwaway credentials."
    return "Demo mode with no console token: every page is open. Never run this way in production."


async def _form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise HTTPException(status_code=413, detail="form too large")
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


def create_app(ctx: ConsoleContext, *, rate_limiter: RateLimiter | None = None) -> FastAPI:
    app = FastAPI(title="Approved console", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_SecurityHeaders)
    app.add_middleware(RequestGuard, rate=rate_limiter or RateLimiter())
    static_dir = resources.files("approved.console").joinpath("static")
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    shim = resources.files("approved.console").joinpath("downloads", "hermes-hook-shim.sh")

    base = ctx.auth.base_path

    def redirect(path: str) -> RedirectResponse:
        return RedirectResponse(base + path, status_code=303)

    def html(request: Request, content: str, csrf: tuple[str, bool], status: int = 200):
        response = HTMLResponse(rebase(content, base), status_code=status)
        token, fresh = csrf
        if fresh:
            response.set_cookie(
                CSRF_COOKIE, token, max_age=SESSION_MAX_AGE_S, **ctx.auth.cookie_options()
            )
        return response

    def require_auth(request: Request) -> None:
        if not ctx.auth.is_authenticated(request):
            raise HTTPException(status_code=401, detail="sign in at /login")

    # ------------------------------------------------------------ public

    @app.get("/health")
    def health() -> JSONResponse:
        status = ctx.follow_status()
        worker = {"running": bool(status.get("running")), "chain": status.get("chain")}
        if status.get("degraded"):
            reason = str(status.get("degraded_reason") or "worker-stopped")
            return JSONResponse(
                {"status": "degraded", "reason": reason, "worker": worker}, status_code=503
            )
        return JSONResponse({"status": "ok", "worker": worker})

    @app.get("/policy", response_class=HTMLResponse)
    def policy(request: Request) -> Response:
        csrf = ctx.auth.csrf_for(request)
        page = policy_page(
            csrf=csrf[0],
            demo_note=_demo_note(ctx),
            authed=ctx.auth.enabled and ctx.auth.is_authenticated(request),
        )
        return html(request, page, csrf)

    @app.post("/api/preview")
    def preview(request: Request, body: PreviewIn) -> dict[str, Any]:
        if not ctx.auth.csrf_ok(request, request.headers.get("x-csrf-token")):
            raise HTTPException(status_code=403, detail="missing or stale CSRF token")
        # OFFLINE reviewer only, whatever mode the worker runs in: an unauthenticated page
        # must never be able to spend a model call.
        reviewer = OfflineReviewer()
        verdict = reviewer.review(
            JudgeRequest(
                action_key="preview",
                action_class=body.action_class,
                seq=0,
                summary=body.command or None,
                command=body.command or None,
            )
        )
        METRICS.incr("console.preview")
        return {
            "action_class": body.action_class,
            "decision": verdict.decision.value,
            "reason": verdict.reason(),
            "issues": [
                {"code": i.code.value, "explanation": i.explanation} for i in verdict.issues
            ],
            "reviewer": verdict.reviewer_version,
        }

    @app.get("/downloads/hermes-hook-shim.sh")
    def download_shim() -> PlainTextResponse:
        return PlainTextResponse(
            shim.read_text(encoding="utf-8"),
            media_type="text/x-shellscript",
            headers={"Content-Disposition": 'attachment; filename="hermes-hook-shim.sh"'},
        )

    # ------------------------------------------------------------ auth

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Response:
        if not ctx.auth.enabled or ctx.auth.is_authenticated(request):
            return redirect("/")
        csrf = ctx.auth.csrf_for(request)
        return html(request, login_page(csrf=csrf[0], error=None, demo_note=_demo_note(ctx)), csrf)

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Response:
        form = await _form(request)
        csrf = ctx.auth.csrf_for(request)
        if not ctx.auth.csrf_ok(request, form.get("csrf")):
            page = login_page(csrf=csrf[0], error="Session expired; try again.", demo_note=None)
            return html(request, page, csrf, status=403)
        if not ctx.auth.check_token(form.get("token", "").strip()):
            METRICS.incr("console.login_failed")
            page = login_page(csrf=csrf[0], error="That token is not right.", demo_note=None)
            return html(request, page, csrf, status=401)
        response = redirect("/")
        response.set_cookie(
            SESSION_COOKIE,
            ctx.auth.session_cookie(),
            max_age=SESSION_MAX_AGE_S,
            **ctx.auth.cookie_options(),
        )
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        form = await _form(request)
        if not ctx.auth.csrf_ok(request, form.get("csrf")):
            raise HTTPException(status_code=403, detail="missing or stale CSRF token")
        response = redirect("/login" if ctx.auth.enabled else "/")
        options = ctx.auth.cookie_options()
        response.delete_cookie(
            SESSION_COOKIE,
            path=options["path"],
            secure=options["secure"],
            httponly=True,
            samesite="strict",
        )
        return response

    # ------------------------------------------------------------ authenticated

    @app.get("/", response_class=HTMLResponse)
    def live(request: Request) -> Response:
        if not ctx.auth.is_authenticated(request):
            return redirect("/login")
        csrf = ctx.auth.csrf_for(request)
        fragment = live_fragment(
            load_view(ctx.state_dir), ctx.follow_status(), facade_host=ctx.facade_host
        )
        page = live_page(fragment, csrf=csrf[0], demo_note=_demo_note(ctx), authed=ctx.auth.enabled)
        return html(request, page, csrf)

    @app.get("/partials/live", response_class=HTMLResponse)
    def live_partial(request: Request) -> HTMLResponse:
        require_auth(request)
        return HTMLResponse(
            live_fragment(
                load_view(ctx.state_dir), ctx.follow_status(), facade_host=ctx.facade_host
            )
        )

    @app.get("/metrics")
    def metrics(request: Request) -> dict[str, Any]:
        require_auth(request)
        view = load_view(ctx.state_dir)
        return {
            "counters": METRICS.snapshot(),
            "judge": view.counters,
            "follow": ctx.follow_status(),
            "open_requests": len(view.open_requests),
            "mode": ctx.mode,
        }

    @app.get("/connect", response_class=HTMLResponse)
    def connect_form(request: Request) -> Response:
        if not ctx.auth.is_authenticated(request):
            return redirect("/login")
        csrf = ctx.auth.csrf_for(request)
        page = connect_page(
            csrf=csrf[0],
            demo_note=_demo_note(ctx),
            form={"mode": "maritime", "approver": "operator"},
            bundle=None,
            error=None,
            authed=ctx.auth.enabled,
        )
        return html(request, page, csrf)

    @app.post("/connect", response_class=HTMLResponse)
    async def connect(request: Request) -> Response:
        if not ctx.auth.is_authenticated(request):
            return redirect("/login")
        form = await _form(request)
        csrf = ctx.auth.csrf_for(request)
        if not ctx.auth.csrf_ok(request, form.get("csrf")):
            raise HTTPException(status_code=403, detail="missing or stale CSRF token")
        mode = form.get("mode", "maritime")
        if mode not in ("maritime", "byo"):
            mode = "maritime"
        form["mode"] = mode
        bundle, error = None, None
        try:
            bundle = build_bundle(
                mode=mode,  # type: ignore[arg-type]
                tenant=form.get("tenant", ""),
                facade_url=form.get("facade_url", ""),
                approver=form.get("approver", "operator") or "operator",
            )
        except BundleError as exc:
            error = str(exc)
        page = connect_page(
            csrf=csrf[0],
            demo_note=_demo_note(ctx),
            form=form,
            bundle=bundle,
            error=error,
            authed=ctx.auth.enabled,
        )
        return html(request, page, csrf, status=200 if bundle else 422)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app
