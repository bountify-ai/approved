"""Server-rendered HTML for the console. Every interpolated value goes through :func:`e`.

No template engine: the pages are small, and plain functions keep escaping visible at every
call site. No inline script or style exists anywhere, so the Content-Security-Policy can be
``'self'`` only.
"""

from __future__ import annotations

import html
import time
from typing import Any

from .bundle import SHIM_PATH, ConnectBundle
from .live import LiveView

__all__ = [
    "connect_page",
    "live_fragment",
    "live_page",
    "login_page",
    "policy_page",
]

TRACE_PREFIX = "https://wandb.ai/"


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _shell(
    *,
    title: str,
    current: str,
    body: str,
    csrf: str,
    demo_note: str | None,
    authed: bool,
    scripts: tuple[str, ...] = ("console.js",),
) -> str:
    here = ' aria-current="page"'
    nav = "".join(
        f'<a href="{href}"{here if key == current else ""}>{label}</a>'
        for key, href, label in (
            ("live", "/", "Live"),
            ("policy", "/policy", "Policy"),
            ("connect", "/connect", "Connect"),
        )
    )
    logout = (
        f'<form method="post" action="/logout"><input type="hidden" name="csrf" '
        f'value="{e(csrf)}"><button class="iconbtn" type="submit">Sign out</button></form>'
        if authed
        else ""
    )
    banner = (
        f'<div class="demo-banner" role="note"><div>{e(demo_note)}</div></div>' if demo_note else ""
    )
    script_tags = "".join(f'<script src="/static/{e(s)}" defer></script>' for s in scripts)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="{e(csrf)}">
<meta name="base-path" content="">
<title>{e(title)} · Approved</title>
<link rel="stylesheet" href="/static/console.css">
{script_tags}
</head>
<body>
<header class="topbar"><div class="topbar-inner">
  <a class="brand" href="/">Approved<span class="md">/ approval.md</span></a>
  <span class="badge">hosted</span>
  <nav aria-label="Console">{nav}</nav>
  <span class="spacer"></span>
  <button class="iconbtn" id="theme-toggle" type="button" aria-label="Toggle light and dark">Theme</button>
  {logout}
</div></header>
{banner}
<main class="wrap">
{body}
</main>
<footer class="foot">Approved: hosted approval.md with an advisory AI judge. The judge never
decides; your tap does. Traces and feedback in Weights &amp; Biases Weave.</footer>
</body>
</html>"""


# ---------------------------------------------------------------- live


def _ago(ts: float | None, now: float) -> str:
    if ts is None:
        return "never"
    delta = max(0, int(now - ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    return f"{delta // 3600}h ago"


def _chip(value: str | None, cls: str | None = None) -> str:
    if not value:
        return '<span class="chip none">none</span>'
    return f'<span class="chip {e(cls or value)}">{e(value)}</span>'


def _judge_cell(verdict: str | None, advisory: str) -> str:
    """The judge's verdict, or why the approver decided without one."""
    if advisory == "sent" or (advisory == "unknown" and verdict):
        return _chip(verdict)
    reason = advisory.split(":", 1)[-1] if ":" in advisory else advisory
    return _chip(f"silent: {reason}", "warn")


def _trace(url: str | None) -> str:
    if url and url.startswith(TRACE_PREFIX):
        return f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">trace</a>'
    return '<span class="muted">none</span>'


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def live_fragment(view: LiveView, status: dict[str, Any], *, facade_host: str) -> str:
    now = time.time()
    chain = status.get("chain", "unknown")
    if view.chain_break:
        chain = "chain-break"
    chain_cls = {"verified": "ok", "chain-break": "bad"}.get(chain, "none")
    follow_ok = status.get("last_follow_ok")
    follow_cls = "ok" if follow_ok else ("bad" if follow_ok is False else "none")
    follow_label = "ok" if follow_ok else ("failing" if follow_ok is False else "not yet")
    error = status.get("last_error")
    c = view.counters
    breaker = status.get("breaker", "closed")
    backlog = int(status.get("follow_failures") or 0)
    if breaker != "closed":
        judge_state, judge_cls = f"judge silent: breaker {breaker}", "warn"
    elif backlog:
        judge_state, judge_cls = f"judge silent: backlog ({backlog} follow failures)", "warn"
    elif chain == "chain-break":
        judge_state, judge_cls = "judge silent: chain-break", "bad"
    else:
        judge_state, judge_cls = "judge speaking", "ok"
    silence_items = ", ".join(f"{e(k)} {e(v)}" for k, v in c.get("silence", {}).items())

    cards = f"""
<section class="grid cards" aria-label="Health and counters">
  <div class="panel card"><p class="eyebrow">Facade follow</p>
    <div class="value">{_chip(follow_label, follow_cls)}</div>
    <div class="sub">{e(facade_host)} · last follow {e(_ago(status.get("last_follow_at"), now))}</div>
    {f'<div class="sub">{e(error)}</div>' if error else ""}</div>
  <div class="panel card"><p class="eyebrow">Chain</p>
    <div class="value">{_chip(chain, chain_cls)}</div>
    <div class="sub">head seq {e(max(status.get("head_seq", 0), view.cursor_seq))} ·
      worker {"running" if status.get("running") else "stopped"}</div></div>
  <div class="panel card"><p class="eyebrow">Judge vs human</p>
    <div class="value">{e(_rate(c["agreement_rate"]))}</div>
    <div class="sub">agree {e(c["agree"])} · disagree {e(c["disagree"])} · escalated {e(c["escalated"])}</div></div>
  <div class="panel card"><p class="eyebrow">False READY</p>
    <div class="value">{e(c["false_ready"])} <span class="small muted">of {e(c["rejected"])} rejected</span></div>
    <div class="sub">escalation rate {e(_rate(c["escalation_rate"]))} · {e(c["verdicts"])} verdicts · {e(c["absent"])} absent</div></div>
  <div class="panel card"><p class="eyebrow">Judge</p>
    <div class="value">{_chip(judge_state, judge_cls)}</div>
    <div class="sub">silence by reason: {silence_items or "none"}</div>
    {f'<div class="sub">record-unverifiable: {e(c["record_unverifiable"])} (not judged, followed past)</div>' if c.get("record_unverifiable") else ""}</div>
</section>"""

    if view.open_requests:
        rows = "".join(
            f"""<tr><td class="num">{e(r.seq)}</td><td><code>{e(r.action_class or "?")}</code></td>
<td class="cmd">{e(r.summary or "")}</td><td>{_chip(r.verdict)}</td>
<td class="small muted">{e(r.judge_status)}</td><td>{_trace(r.trace_url)}</td></tr>"""
            for r in view.open_requests
        )
        open_html = f"""<div class="table-wrap"><table><thead><tr><th>Seq</th><th>Class</th>
<th>Request</th><th>Judge</th><th>Advisory</th><th>Trace</th></tr></thead><tbody>{rows}</tbody></table></div>"""
    else:
        open_html = '<p class="empty">No open requests. Decisions show up below as they land.</p>'

    if view.recent:
        rows = "".join(
            f"""<tr><td class="num">{e(d.seq)}</td><td><code>{e(d.action_class or "?")}</code></td>
<td>{_chip(d.human)}</td><td>{_judge_cell(d.verdict, d.advisory)}</td><td>{_chip(d.label)}</td>
<td class="small muted">{e(d.actor)}</td><td>{_trace(d.trace_url)}</td>
<td class="small muted">{e(d.feedback)}</td></tr>"""
            for d in view.recent
        )
        recent_html = f"""<div class="table-wrap"><table><thead><tr><th>Seq</th><th>Class</th>
<th>Human</th><th>Judge</th><th>Label</th><th>By</th><th>Trace</th><th>Feedback</th></tr></thead>
<tbody>{rows}</tbody></table></div>"""
    else:
        recent_html = '<p class="empty">No decisions yet.</p>'

    unreadable = (
        ""
        if view.readable
        else '<p class="error">The judge has not written its state yet (or it cannot be read).</p>'
    )
    return f"""{unreadable}{cards}
<section class="panel"><div class="panel-head"><p class="eyebrow">Open requests</p></div>{open_html}</section>
<section class="panel"><div class="panel-head"><p class="eyebrow">Recent decisions</p></div>{recent_html}</section>"""


def live_page(fragment: str, *, csrf: str, demo_note: str | None, authed: bool) -> str:
    body = f"""<div class="page-head"><h1>Live tenant view</h1>
<p>What the judge has seen on this tenant's log: follow health, open requests with the judge's
advisory, and how the judge compares with the human's decisions.
<span class="small muted" id="live-updated"></span></p></div>
<div id="live" data-refresh-ms="3000">{fragment}</div>"""
    return _shell(
        title="Live", current="live", body=body, csrf=csrf, demo_note=demo_note, authed=authed
    )


# ---------------------------------------------------------------- login


def login_page(*, csrf: str, error: str | None, demo_note: str | None) -> str:
    err = f'<p class="error" role="alert">{e(error)}</p>' if error else ""
    body = f"""<section class="panel login"><p class="eyebrow">Operator sign-in</p>{err}
<form method="post" action="/login">
  <input type="hidden" name="csrf" value="{e(csrf)}">
  <label for="token">Console token</label>
  <input id="token" name="token" type="password" autocomplete="current-password" required>
  <div class="row"><button class="primary" type="submit">Sign in</button></div>
  <p class="hint">The token is the CONSOLE_TOKEN this service was started with.</p>
</form></section>"""
    return _shell(
        title="Sign in", current="", body=body, csrf=csrf, demo_note=demo_note, authed=False
    )


# ---------------------------------------------------------------- policy


def policy_page(*, csrf: str, demo_note: str | None, authed: bool) -> str:
    body = """<div class="page-head"><h1>Policy builder</h1>
<p>One <code>APPROVAL.md</code> for one tenant. Fill in the approver and the classes, download
the file into the tenant's store, and attest it as the human approver
(<code>approval policy attest --as human:&lt;you&gt;</code>). Ported from the approval.md hosted
policy builder.</p></div>
<div class="grid two">
  <div>
  <section class="panel">
    <p class="eyebrow">Policy</p>
    <label for="tenant">Tenant slug (names the channel's environment variables)</label>
    <input id="tenant" value="acme" spellcheck="false">
    <label for="approver">Human approver id</label>
    <input id="approver" value="carter" spellcheck="false">
    <label for="sender">Telegram numeric sender id</label>
    <input id="sender" placeholder="123456789" spellcheck="false">
    <label for="defaultAutonomy">Default autonomy, for any class not named below</label>
    <select id="defaultAutonomy">
      <option value="manual" selected>manual</option>
      <option value="supervised-retro">supervised-retro</option>
      <option value="autonomous">autonomous</option>
      <option value="human-only">human-only</option>
    </select>
    <label for="ttl">Approval TTL</label>
    <input id="ttl" value="2h" spellcheck="false">
    <label>Channel</label>
    <div class="fixed"><span class="dot"></span><b>telegram</b>, the tenant's own bot</div>
    <label>Classes</label>
    <table class="pb-table"><thead><tr><th>Class</th><th>Autonomy</th><th></th></tr></thead>
      <tbody id="rows"></tbody></table>
    <div class="row"><button id="add" type="button">+ Add class</button></div>
    <p class="hint">Strictest first: <code>human-only</code>, <code>manual</code>,
    <code>supervised-retro</code>, <code>autonomous</code>. <code>supervised-live</code> is left out
    because it needs a <code>live_rate</code> this form does not collect.</p>
  </section>
  <section class="panel">
    <p class="eyebrow">What the judge would say</p>
    <label for="preview-class">Class</label>
    <select id="preview-class"></select>
    <label for="preview-command">Sample command or summary</label>
    <textarea id="preview-command" spellcheck="false"></textarea>
    <div class="row"><button id="preview-run" class="primary" type="button">Ask the judge</button></div>
    <pre class="advisory" id="preview-result" hidden></pre>
    <p class="hint">This page runs the offline rules-based reviewer only. It never calls a model:
    the live judge (W&amp;B Inference) runs on your tenant's requests, not on this form.</p>
  </section>
  </div>
  <section class="panel">
    <div class="panel-head"><p class="eyebrow">APPROVAL.md</p>
      <div class="row"><button type="button" data-copy="out">Copy</button>
      <button id="download" class="primary" type="button">Download APPROVAL.md</button></div></div>
    <pre class="code" id="out"></pre>
    <p class="hint">The runtime reads only the fenced <code>yaml approval-policy</code> block.
    Secrets never appear here: the policy names environment variables.</p>
  </section>
</div>"""
    return _shell(
        title="Policy",
        current="policy",
        body=body,
        csrf=csrf,
        demo_note=demo_note,
        authed=authed,
        scripts=("console.js", "policy.js"),
    )


# ---------------------------------------------------------------- connect


def connect_page(
    *,
    csrf: str,
    demo_note: str | None,
    form: dict[str, str],
    bundle: ConnectBundle | None,
    error: str | None,
    authed: bool,
) -> str:
    mode = form.get("mode", "maritime")
    err = f'<p class="error" role="alert">{e(error)}</p>' if error else ""

    def checked(value: str) -> str:
        return " checked" if mode == value else ""

    form_html = f"""<section class="panel"><p class="eyebrow">Tenant</p>{err}
<form method="post" action="/connect">
  <input type="hidden" name="csrf" value="{e(csrf)}">
  <label for="tenant">Tenant name</label>
  <input id="tenant" name="tenant" value="{e(form.get("tenant", ""))}" placeholder="acme" required spellcheck="false">
  <label for="facade_url">Facade URL (the daemon machine's public URL)</label>
  <input id="facade_url" name="facade_url" value="{e(form.get("facade_url", ""))}" placeholder="https://api.maritime.sh/a/&lt;daemon-agent-id&gt;" required spellcheck="false">
  <label for="approver">Human approver id</label>
  <input id="approver" name="approver" value="{e(form.get("approver", "operator"))}" spellcheck="false">
  <label>Mode</label>
  <label class="radio"><input type="radio" name="mode" value="maritime"{checked("maritime")}>
    <span>Approved agent on Maritime<small>The daemon plus a gated Hermes machine, both on Maritime.</small></span></label>
  <label class="radio"><input type="radio" name="mode" value="byo"{checked("byo")}>
    <span>Bring your own Hermes<small>The daemon on Maritime; the hook shim on your own Hermes.</small></span></label>
  <div class="row"><button class="primary" type="submit">Build the bundle</button></div>
  <p class="hint">No credential is generated or shown here. The bundle is a script you run on
  your own machine; it writes fresh credentials into mode-0600 files.</p>
</form></section>"""

    if bundle is None:
        out = """<section class="panel"><p class="eyebrow">Bundle</p>
<p class="empty">Fill in the tenant and pick a mode.</p></section>"""
    else:
        hooks = (
            f"""<section class="panel"><div class="panel-head"><p class="eyebrow">$HERMES_HOME/config.yaml</p>
<button type="button" data-copy="hooks">Copy</button></div><pre class="code" id="hooks">{e(bundle.hooks_yaml)}</pre>
<p class="hint">Hook shim: <a href="{e(SHIM_PATH)}">download hermes-hook-shim.sh</a>.</p></section>"""
            if bundle.hooks_yaml
            else ""
        )
        notes = "".join(f"<li>{e(n)}</li>" for n in bundle.notes)
        out = f"""<section class="panel"><div class="panel-head"><p class="eyebrow">{e(bundle.title)}: connect-{e(bundle.tenant)}.sh</p>
<button type="button" data-copy="script">Copy</button></div>
<pre class="code" id="script">{e(bundle.script)}</pre><ul class="notes">{notes}</ul></section>{hooks}"""

    body = f"""<div class="page-head"><h1>Connect a tenant</h1>
<p>The exact commands to stand up a gated tenant on Maritime, with placeholders where a person
must supply a value and a local command that generates the credentials.</p></div>
<div class="grid two">{form_html}<div>{out}</div></div>"""
    return _shell(
        title="Connect", current="connect", body=body, csrf=csrf, demo_note=demo_note, authed=authed
    )
