"use strict";
// Approved console: the session in a request header, for proxies that drop the Cookie header.
//
// Maritime's public proxy drops the Cookie request header (and Authorization) but passes
// custom headers. This script signs in with a fetch that carries X-Approved-Login. When the
// server sees no CSRF cookie on that request (the proxy dropped it), it answers with the
// session value in the JSON body instead of a cookie; this script keeps it in THIS TAB's
// sessionStorage (never localStorage) and sends it as X-Approved-Session on its own requests.
// Where cookies work, the server answers {"flow": "cookie"} and sets the cookie as before.
// The session value never goes into a URL, a log line or the page.
//
// A browser navigation cannot carry a header. So in header mode every console page arrives
// as the sign-in document (the server redirects a header-less GET of a signed-in page to
// /login), and this script fetches the page the URL fragment names with the header and swaps
// it in. The fragment holds a page path only ("#/connect"); it survives the server's
// redirect, so reload and Back land on the same page.
//
// Loaded synchronously in <head>, before the body renders, so a page about to be swapped is
// hidden (html.approved-pending) instead of flashing the sign-in form.
(function (root) {
  var SESSION_HEADER = "X-Approved-Session";
  var LOGIN_HEADER = "X-Approved-Login";
  var PAGES = { "/": 1, "/policy": 1, "/connect": 1, "/metrics": 1 };
  var FORMS = { "/login": 1, "/logout": 1, "/connect": 1 };
  var own = Object.prototype.hasOwnProperty;

  // ------------------------------------------------------------ pure helpers

  // A path relative to the console's base, when it is one of `known`; otherwise null. Anything
  // with a query or a fragment, another agent's path, or another origin is never ours.
  function consolePath(href, base, known) {
    if (typeof href !== "string" || /[?#]/.test(href)) return null;
    var path = href;
    if (base) {
      if (path === base) path = "/";
      else if (path.indexOf(base + "/") === 0) path = path.slice(base.length);
      else return null;
    }
    return own.call(known, path) ? path : null;
  }

  function targetFromHash(hash) {
    var path = String(hash || "").replace(/^#/, "");
    return own.call(PAGES, path) ? path : null;
  }

  // The page to fetch into this document: the fragment's page, else this URL's own page,
  // else the live view (the sign-in document itself is never a target).
  function pageTarget(hash, pathname, base) {
    return targetFromHash(hash) || consolePath(pathname, base, PAGES) || "/";
  }

  function needsSwap(flow, hasSession) {
    return !!hasSession && flow === "none";
  }

  var pure = {
    consolePath: consolePath,
    targetFromHash: targetFromHash,
    pageTarget: pageTarget,
    needsSwap: needsSwap,
    PAGES: PAGES,
    FORMS: FORMS
  };
  if (typeof module === "object" && module && module.exports) module.exports = pure;
  if (!root || !root.document) return;

  // ------------------------------------------------------------ this document

  var doc = root.document;

  function meta(name) {
    var m = doc.querySelector('meta[name="' + name + '"]');
    return m ? m.getAttribute("content") || "" : "";
  }
  function setMeta(name, value) {
    var m = doc.querySelector('meta[name="' + name + '"]');
    if (m) m.setAttribute("content", value);
  }
  function base() { return meta("base-path"); }
  // Scoped by base path: two consoles on one shared origin, in one tab, keep separate values.
  function key() { return "approved-session:" + (base() || "/"); }
  function storage() {
    try { return root.sessionStorage || null; } catch (e) { return null; }
  }
  function stored() {
    var s = storage();
    if (!s) return null;
    try { return s.getItem(key()); } catch (e) { return null; }
  }
  function remember(value) {
    var s = storage();
    if (!s) return false;
    try { s.setItem(key(), value); return s.getItem(key()) === value; } catch (e) { return false; }
  }
  function forget() {
    var s = storage();
    if (!s) return;
    try { s.removeItem(key()); } catch (e) { /* nothing kept */ }
  }
  function headerMode() {
    var flow = meta("session-flow");
    return !!stored() && flow !== "cookie" && flow !== "open";
  }
  function here() { return consolePath(root.location.pathname, base(), PAGES); }
  function onLoginDoc() { return consolePath(root.location.pathname, base(), FORMS) === "/login"; }

  function sessionHeaders(extra) {
    var h = {};
    Object.keys(extra || {}).forEach(function (k) { h[k] = extra[k]; });
    if (headerMode()) h[SESSION_HEADER] = stored();
    return h;
  }

  // ------------------------------------------------------------ page callbacks

  // Page scripts register their setup here. It runs once the document holds the page it will
  // show (at DOMContentLoaded, or after a swap), and again after every later swap.
  var callbacks = [];
  var ready = false;
  function safely(fn) {
    try { fn(); } catch (e) { if (root.console) root.console.error(e); }
  }
  function onPage(fn) {
    callbacks.push(fn);
    if (ready) safely(fn);
  }
  function runPage() {
    ready = true;
    callbacks.slice().forEach(safely);
  }

  var html = doc.documentElement;
  function reveal() { html.classList.remove("approved-pending"); }

  function notice(message, form) {
    var holder = (form && form.parentNode) || doc.querySelector("main");
    if (!holder) return;
    var p = holder.querySelector(".error");
    if (!p) {
      p = doc.createElement("p");
      p.className = "error";
      p.setAttribute("role", "alert");
      holder.insertBefore(p, form || holder.firstChild);
    }
    p.textContent = message;
  }

  // ------------------------------------------------------------ header mode

  function expired(rel) {
    forget();
    if (!ready && onLoginDoc()) { // the sign-in document, still hidden: just show it
      reveal();
      runPage();
      return;
    }
    root.location.assign(base() + "/login#" + (rel || here() || "/"));
  }

  // The answer came from this console: same origin, and a path under this console's own base.
  // On Maritime's shared origin every agent is same-origin, so the origin alone proves nothing.
  function fromConsole(r) {
    try {
      var u = new root.URL(r.url, root.location.href);
      var b = base();
      return u.origin === root.location.origin && (u.pathname === b || u.pathname.indexOf(b + "/") === 0);
    } catch (e) {
      return false;
    }
  }

  var SCRIPT_NAME = /^[a-z]+\.js$/;

  function swap(text) {
    var next = new root.DOMParser().parseFromString(text, "text/html");
    ["csrf-token", "session-flow"].forEach(function (name) {
      var m = next.querySelector('meta[name="' + name + '"]');
      if (m) setMeta(name, m.getAttribute("content") || "");
    });
    doc.title = next.title;
    doc.body.replaceWith(doc.adoptNode(next.body));
    // The new page's scripts that this document has not loaded yet (policy.js after the
    // sign-in document). The CSP's script-src 'self' does not narrow this: on a shared origin
    // it admits every agent's paths. What does: the page came from this console (swap runs
    // only on fromConsole answers), and a script is added only as <base>/static/<name>.js.
    var prefix = base() + "/static/";
    var have = {};
    Array.prototype.forEach.call(doc.querySelectorAll("script[src]"), function (s) {
      have[s.getAttribute("src")] = true;
    });
    Array.prototype.forEach.call(next.querySelectorAll("script[src]"), function (s) {
      var src = s.getAttribute("src") || "";
      if (have[src] || src.indexOf(prefix) !== 0 || !SCRIPT_NAME.test(src.slice(prefix.length))) return;
      var el = doc.createElement("script");
      el.src = src;
      doc.head.appendChild(el);
    });
  }

  function showJson(rel, text) {
    var main = doc.querySelector("main");
    if (!main) return;
    var pretty = text;
    try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch (e) { /* as sent */ }
    main.textContent = "";
    var h = doc.createElement("h1");
    h.textContent = rel === "/metrics" ? "Metrics" : rel;
    var pre = doc.createElement("pre");
    pre.className = "code";
    pre.textContent = pretty;
    main.appendChild(h);
    main.appendChild(pre);
    doc.title = h.textContent + " · Approved";
    setMeta("session-flow", "header");
  }

  function failed(message) {
    reveal();
    if (!ready) runPage();
    notice(message);
  }

  function loadInto(rel) {
    if (!own.call(PAGES, rel)) rel = "/"; // only this console's own pages, under its base
    var headers = {};
    headers[SESSION_HEADER] = stored();
    return root.fetch(base() + rel, { credentials: "same-origin", cache: "no-store", headers: headers })
      .then(function (r) {
        if (r.status === 401) { expired(rel); return null; }
        var type = r.headers.get("content-type") || "";
        var json = type.indexOf("application/json") === 0;
        var html = type.indexOf("text/html") === 0;
        if (!r.ok || !fromConsole(r) || !(json || html)) {
          failed("The console answered HTTP " + r.status + "; reload to try again.");
          return null;
        }
        return r.text().then(function (text) {
          if (json) showJson(rel, text);
          else swap(text);
          root.history.replaceState(null, "", base() + rel + "#" + rel);
          reveal();
          runPage();
        });
      })
      .catch(function () { failed("Console unreachable; reload to try again."); });
  }

  function formBody(form) {
    return new root.URLSearchParams(new root.FormData(form));
  }

  function readJson(r) {
    return r.json().then(
      function (body) { return { status: r.status, own: fromConsole(r), body: body || {} }; },
      function () { return { status: r.status, own: fromConsole(r), body: {} }; }
    );
  }

  function signIn(form) {
    var body = formBody(form);
    var token = form.querySelector('input[name="token"]');
    if (token) token.value = "";
    var headers = {};
    headers[LOGIN_HEADER] = "1";
    return root.fetch(form.getAttribute("action"), {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: headers,
      body: body
    })
      .then(readJson)
      .then(function (res) {
        var target = targetFromHash(root.location.hash) || "/";
        if (res.own && res.status === 200 && res.body.flow === "header" && typeof res.body.session === "string") {
          if (!remember(res.body.session)) {
            notice("This browser keeps no session storage for this page, so the console cannot keep you signed in.", form);
            return null;
          }
          return loadInto(target);
        }
        if (res.own && res.status === 200 && res.body.flow === "cookie") {
          forget();
          root.location.assign(base() + target);
          return null;
        }
        if (!res.own) {
          notice("Sign-in failed (HTTP " + res.status + ").", form);
          return null;
        }
        if (res.status === 403) { // a stale CSRF cookie: a fresh sign-in page carries a new one
          root.location.reload();
          return null;
        }
        notice(res.body.detail || "Sign-in failed (HTTP " + res.status + ").", form);
        return null;
      })
      .catch(function () { notice("Console unreachable; try again.", form); });
  }

  function post(form, rel) {
    return root.fetch(form.getAttribute("action"), {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: sessionHeaders({}),
      body: formBody(form)
    })
      .then(function (r) {
        if (r.status === 401) { expired(here() || "/"); return null; }
        if (rel === "/logout") {
          if (r.ok && fromConsole(r)) {
            forget();
            root.location.assign(base() + "/login");
          } else {
            notice("Sign-out failed (HTTP " + r.status + ").");
          }
          return null;
        }
        var type = r.headers.get("content-type") || "";
        if (r.ok && fromConsole(r) && type.indexOf("text/html") === 0) {
          return r.text().then(function (text) {
            swap(text);
            runPage();
          });
        }
        // Anything else: a fixed line, never the answer's own content.
        notice(r.status === 422
          ? "The console could not build that bundle (HTTP 422): check the tenant name and the facade URL."
          : "The console refused that (HTTP " + r.status + ").");
        return null;
      })
      .catch(function () { notice("Console unreachable; try again."); });
  }

  // ------------------------------------------------------------ wiring

  var flowAtLoad = meta("session-flow");
  if ((flowAtLoad === "cookie" || flowAtLoad === "open") && stored()) forget(); // cookies work here
  var pending = needsSwap(flowAtLoad, !!stored());
  if (pending) {
    html.classList.add("approved-pending");
    root.setTimeout(reveal, 10000); // never leave a blank page behind
  }

  doc.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form || !form.getAttribute || typeof root.fetch !== "function") return;
    if (typeof root.FormData !== "function" || typeof root.URLSearchParams !== "function") return;
    if ((form.getAttribute("method") || "").toLowerCase() !== "post") return;
    var rel = consolePath(form.getAttribute("action") || "", base(), FORMS);
    if (rel === "/login") {
      ev.preventDefault();
      signIn(form);
    } else if (rel && headerMode()) {
      ev.preventDefault();
      post(form, rel);
    }
  });

  doc.addEventListener("click", function (ev) {
    if (!headerMode() || ev.defaultPrevented || ev.button !== 0) return;
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    var a = ev.target && ev.target.closest ? ev.target.closest("a[href]") : null;
    if (!a || a.getAttribute("target") || a.hasAttribute("download")) return;
    var rel = consolePath(a.getAttribute("href"), base(), PAGES);
    if (!rel) return;
    ev.preventDefault();
    root.location.assign(base() + "/login#" + rel);
  });

  doc.addEventListener("DOMContentLoaded", function () {
    if (pending) loadInto(pageTarget(root.location.hash, root.location.pathname, base()));
    else runPage();
  });

  root.ApprovedSession = {
    onPage: onPage,
    headers: sessionHeaders,
    expired: function () { expired(here() || "/"); },
    base: base
  };
})(typeof window === "undefined" ? null : window);
