"use strict";
// Unit-level harness for console/static/session.js (and console.js's use of it), run by
// tests/test_console_session_header.py::test_session_script_under_node.
//
// NOT a browser. The DOM, fetch, sessionStorage, history and DOMParser here are small stubs
// that model only what the scripts call; HTML is never parsed (the stub DOMParser maps a
// marker string to a prebuilt page). It proves the scripts' own logic: which requests carry
// which headers, what is stored where, which URLs are visited, and that the session value
// and the token never land in a URL or in localStorage. Rendering, the real CSP, real
// redirects and real sessionStorage semantics are not exercised.
//
// Usage: node session_harness.js <path to console/static>
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC = process.argv[2] && path.resolve(process.argv[2]);
if (!STATIC) throw new Error("usage: node session_harness.js <console/static dir>");
const SESSION_SRC = fs.readFileSync(path.join(STATIC, "session.js"), "utf8");
const CONSOLE_SRC = fs.readFileSync(path.join(STATIC, "console.js"), "utf8");

const BASE = "/a/x";
const TOKEN = "TOKEN-console-9f8e7d6c5b4a";
const SESSION = "1790683230." + "ab12".repeat(16);
const KEY = "approved-session:" + BASE;

let passed = 0;
function check(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => { passed += 1; console.log("ok   " + name); })
    .catch((err) => { console.log("FAIL " + name); throw err; });
}

// ------------------------------------------------------------------ pure helpers

const pure = require(path.join(STATIC, "session.js"));

function pureChecks() {
  const { consolePath, targetFromHash, pageTarget, needsSwap, PAGES, FORMS } = pure;
  assert.strictEqual(consolePath("/a/x/connect", BASE, PAGES), "/connect");
  assert.strictEqual(consolePath("/a/x/", BASE, PAGES), "/");
  assert.strictEqual(consolePath("/a/x", BASE, PAGES), "/");
  assert.strictEqual(consolePath("/a/x/metrics", BASE, PAGES), "/metrics");
  assert.strictEqual(consolePath("/a/y/connect", BASE, PAGES), null, "another agent");
  assert.strictEqual(consolePath("/a/xy/connect", BASE, PAGES), null, "a longer prefix");
  assert.strictEqual(consolePath("https://wandb.ai/b/j/r/call/1", BASE, PAGES), null);
  assert.strictEqual(consolePath("//evil.test/a/x/connect", BASE, PAGES), null);
  assert.strictEqual(consolePath("/a/x/connect?next=//evil", BASE, PAGES), null);
  assert.strictEqual(consolePath("/a/x/connect#x", BASE, PAGES), null);
  assert.strictEqual(consolePath("/a/x/static/console.js", BASE, PAGES), null);
  assert.strictEqual(consolePath("/a/x/downloads/hermes-hook-shim.sh", BASE, PAGES), null);
  assert.strictEqual(consolePath("/a/x/login", BASE, PAGES), null);
  assert.strictEqual(consolePath("/a/x/login", BASE, FORMS), "/login");
  assert.strictEqual(consolePath("/connect", "", PAGES), "/connect");
  assert.strictEqual(consolePath("/", "", PAGES), "/");
  assert.strictEqual(consolePath(null, BASE, PAGES), null);
  assert.strictEqual(targetFromHash("#/connect"), "/connect");
  assert.strictEqual(targetFromHash("#/"), "/");
  assert.strictEqual(targetFromHash("#//evil.test"), null);
  assert.strictEqual(targetFromHash("#javascript:alert(1)"), null);
  assert.strictEqual(targetFromHash("#/a/x/connect"), null);
  assert.strictEqual(targetFromHash("#/login"), null);
  assert.strictEqual(targetFromHash(""), null);
  assert.strictEqual(pageTarget("", "/a/x/login", BASE), "/");
  assert.strictEqual(pageTarget("#/metrics", "/a/x/login", BASE), "/metrics");
  assert.strictEqual(pageTarget("", "/a/x/policy", BASE), "/policy");
  assert.strictEqual(pageTarget("#evil", "/a/x/policy", BASE), "/policy");
  assert.strictEqual(needsSwap("none", true), true);
  for (const flow of ["header", "cookie", "open"]) assert.strictEqual(needsSwap(flow, true), false);
  assert.strictEqual(needsSwap("none", false), false);
}

// ------------------------------------------------------------------ a small DOM stub

const SELECTOR = /^([a-z]*)(?:\[([a-z-]+)(?:="([^"]*)")?\])?$/;

function matches(el, sel) {
  if (sel.startsWith(".")) return el.classes.has(sel.slice(1));
  const m = SELECTOR.exec(sel);
  if (!m) throw new Error("stub selector not supported: " + sel);
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  if (m[2] && !(m[2] in el.attrs)) return false;
  if (m[3] !== undefined && el.attrs[m[2]] !== m[3]) return false;
  return true;
}

class El {
  constructor(tag, attrs, children) {
    this.tagName = tag.toUpperCase();
    this.attrs = Object.assign({}, attrs || {});
    this.classes = new Set((this.attrs.class || "").split(/\s+/).filter(Boolean));
    this.children = [];
    this.parentNode = null;
    this.listeners = {};
    this.text = "";
    this.value = this.attrs.value || "";
    const self = this;
    this.classList = {
      add: (c) => self.classes.add(c),
      remove: (c) => self.classes.delete(c),
      contains: (c) => self.classes.has(c)
    };
    (children || []).forEach((c) => this.appendChild(typeof c === "string" ? text(c) : c));
  }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  hasAttribute(n) { return n in this.attrs; }
  get className() { return Array.from(this.classes).join(" "); }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get src() { return this.attrs.src; }
  set src(v) { this.attrs.src = String(v); }
  get content() { return this.attrs.content; }
  get id() { return this.attrs.id; }
  get firstChild() { return this.children[0] || null; }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.children = []; this.text = String(v); }
  appendChild(c) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this;
    this.children.push(c);
    return c;
  }
  insertBefore(c, ref) {
    if (!ref) return this.appendChild(c);
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this;
    this.children.splice(this.children.indexOf(ref), 0, c);
    return c;
  }
  removeChild(c) {
    this.children.splice(this.children.indexOf(c), 1);
    c.parentNode = null;
    return c;
  }
  replaceWith(n) {
    const p = this.parentNode;
    if (n.parentNode) n.parentNode.removeChild(n);
    p.children[p.children.indexOf(this)] = n;
    n.parentNode = p;
    this.parentNode = null;
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  closest(sel) {
    for (let e = this; e && e.tagName; e = e.parentNode) if (matches(e, sel)) return e;
    return null;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (e) => e.children.forEach((c) => { if (matches(c, sel)) out.push(c); walk(c); });
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function text(s) {
  const t = new El("#text");
  t.text = s;
  return t;
}
const h = (tag, attrs, ...children) => new El(tag, attrs, children);

// A console page as the server renders it, reduced to what the scripts touch.
function page(opts) {
  const b = opts.base === undefined ? BASE : opts.base;
  const scripts = [b + "/static/session.js", b + "/static/console.js"].concat(opts.extraScripts || []);
  const head = h(
    "head",
    {},
    h("meta", { name: "csrf-token", content: opts.csrf || "cookie-csrf-aaaaaaaaaaaaaaaa" }),
    h("meta", { name: "base-path", content: b }),
    h("meta", { name: "session-flow", content: opts.flow }),
    ...scripts.map((src) => h("script", { src: src }))
  );
  const nav = h(
    "nav",
    {},
    h("a", { href: b + "/" }, "Live"),
    h("a", { href: b + "/policy" }, "Policy"),
    h("a", { href: b + "/connect" }, "Connect")
  );
  const signOut = opts.authed
    ? h("form", { method: "post", action: b + "/logout" }, h("input", { type: "hidden", name: "csrf", value: opts.csrf }))
    : null;
  const header = h("header", {}, h("button", { id: "theme-toggle", type: "button" }, "Theme"), nav);
  if (signOut) header.appendChild(signOut);
  const main = h("main", {}, ...(opts.main || []));
  const body = h("body", {}, header, main);
  const html = h("html", {}, head, body);
  return { title: opts.title || "Page · Approved", html: html, head: head };
}

function loginMain() {
  return [
    h(
      "section",
      { class: "panel login" },
      h(
        "form",
        { method: "post", action: BASE + "/login" },
        h("input", { type: "hidden", name: "csrf", value: "cookie-csrf-aaaaaaaaaaaaaaaa" }),
        h("input", { name: "token", type: "password" })
      )
    )
  ];
}

const PAGES = {
  login: () => page({ flow: "none", title: "Sign in · Approved", main: loginMain() }),
  "live-header": () =>
    page({
      flow: "header",
      authed: true,
      csrf: "session-csrf-1",
      title: "Live · Approved",
      main: [
        h("p", {}, h("a", { href: BASE + "/metrics" }, "JSON counters"), h("span", { id: "live-updated" })),
        h("div", { id: "live", "data-refresh-ms": "3000" }, "cards"),
        h("a", { href: "https://wandb.ai/b/j/r/call/1", target: "_blank" }, "trace")
      ]
    }),
  "connect-header": () =>
    page({
      flow: "header",
      authed: true,
      csrf: "session-csrf-1",
      title: "Connect · Approved",
      main: [
        h(
          "form",
          { method: "post", action: BASE + "/connect" },
          h("input", { type: "hidden", name: "csrf", value: "session-csrf-1" }),
          h("input", { name: "tenant", value: "acme" })
        ),
        h("a", { href: BASE + "/downloads/hermes-hook-shim.sh" }, "shim")
      ]
    }),
  "connect-bundle": () =>
    page({
      flow: "header",
      authed: true,
      csrf: "session-csrf-1",
      title: "Connect · Approved",
      main: [h("pre", { id: "script" }, "connect-acme.sh bundle")]
    }),
  "policy-public": () => page({ flow: "none", title: "Policy · Approved", extraScripts: [BASE + "/static/policy.js"] }),
  "policy-header": () =>
    page({
      flow: "header",
      authed: true,
      csrf: "session-csrf-1",
      title: "Policy · Approved",
      extraScripts: [BASE + "/static/policy.js"],
      main: [h("tbody", { id: "rows" })]
    }),
  "live-cookie": () => page({ flow: "cookie", authed: true, title: "Live · Approved", main: [h("div", { id: "live" })] })
};

// ------------------------------------------------------------------ a browser-ish realm

function realm(opts) {
  const start = PAGES[opts.page]();
  const events = { fetches: [], assigns: [], reloads: 0, replaced: [], intervals: [], cleared: [], appended: [] };
  const docListeners = {};
  const document = {
    documentElement: start.html,
    head: start.head,
    title: start.title,
    get body() { return start.html.children.find((c) => c.tagName === "BODY"); },
    querySelector: (s) => start.html.querySelector(s),
    querySelectorAll: (s) => start.html.querySelectorAll(s),
    getElementById: (id) => start.html.querySelector('[id="' + id + '"]'),
    createElement: (tag) => new El(tag),
    adoptNode: (n) => n,
    addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); }
  };
  const origAppend = start.head.appendChild.bind(start.head);
  start.head.appendChild = (c) => { events.appended.push(c.getAttribute("src")); return origAppend(c); };

  const store = new Map(opts.stored ? [[KEY, opts.stored]] : []);
  const sessionStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k)
  };
  const local = new Map();
  const localStorage = {
    getItem: (k) => (local.has(k) ? local.get(k) : null),
    setItem: (k, v) => local.set(k, String(v)),
    removeItem: (k) => local.delete(k)
  };
  const location = {
    pathname: opts.pathname || BASE + "/login",
    hash: opts.hash || "",
    assign: (u) => events.assigns.push(u),
    reload: () => { events.reloads += 1; }
  };
  const history = {
    replaceState: (_s, _t, url) => {
      events.replaced.push(url);
      const [p, frag] = url.split("#");
      location.pathname = p;
      location.hash = frag === undefined ? "" : "#" + frag;
    }
  };
  function fetch(url, init) {
    const call = { url: url, init: init || {} };
    events.fetches.push(call);
    const answer = opts.server(call);
    const res = new Response(answer.body === undefined ? "" : answer.body, {
      status: answer.status || 200,
      headers: { "content-type": answer.type || "text/html; charset=utf-8" }
    });
    return Promise.resolve(res);
  }
  class DOMParser {
    parseFromString(markup) {
      const name = /^PAGE:(.+)$/.exec(markup);
      assert.ok(name, "the stub parses only PAGE:<name> markers, got " + markup.slice(0, 40));
      const p = PAGES[name[1]]();
      return {
        title: p.title,
        body: p.html.children.find((c) => c.tagName === "BODY"),
        querySelector: (s) => p.html.querySelector(s),
        querySelectorAll: (s) => p.html.querySelectorAll(s)
      };
    }
  }
  class FormData {
    constructor(form) {
      this.pairs = form.querySelectorAll("input").filter((i) => i.getAttribute("name")).map((i) => [i.getAttribute("name"), i.value]);
    }
    [Symbol.iterator]() { return this.pairs[Symbol.iterator](); }
  }
  const g = {
    document, sessionStorage, localStorage, location, history, fetch, DOMParser, FormData,
    URLSearchParams, Response, console, navigator: {},
    setTimeout: () => 0,
    setInterval: (fn) => { events.intervals.push(fn); return events.intervals.length; },
    clearInterval: (id) => { events.cleared.push(id); }
  };
  g.window = g;
  vm.createContext(g);
  vm.runInContext(SESSION_SRC, g, { filename: "session.js" });
  vm.runInContext(CONSOLE_SRC, g, { filename: "console.js" });

  function dispatch(type, target, extra) {
    const ev = Object.assign(
      { type: type, target: target, button: 0, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } },
      extra || {}
    );
    (docListeners[type] || []).forEach((fn) => fn(ev));
    return ev;
  }
  return {
    g, document, events, store, local, location,
    ready: () => dispatch("DOMContentLoaded", document),
    submit: (form) => dispatch("submit", form),
    click: (el, extra) => dispatch("click", el, extra),
    pending: () => start.html.classes.has("approved-pending"),
    meta: (n) => document.querySelector('meta[name="' + n + '"]').getAttribute("content")
  };
}

function settle() {
  let p = Promise.resolve();
  for (let i = 0; i < 20; i += 1) p = p.then(() => new Promise((r) => setImmediate(r)));
  return p;
}

// Every URL any realm visited or fetched, and every request body: checked at the end.
const allRealms = [];
function track(r) { allRealms.push(r); return r; }

function json(body, status) {
  return { status: status || 200, type: "application/json", body: JSON.stringify(body) };
}

// ------------------------------------------------------------------ scenarios

async function main() {
  await check("pure helpers", pureChecks);

  await check("sign-in behind the proxy: header flow, sessionStorage, fragment target", async () => {
    const r = track(realm({
      page: "login",
      hash: "#/connect",
      server: (c) => {
        if (c.url === BASE + "/login") return json({ flow: "header", session: SESSION });
        if (c.url === BASE + "/connect") return { body: "PAGE:connect-header" };
        throw new Error("unexpected fetch " + c.url);
      }
    }));
    assert.strictEqual(r.pending(), false, "nothing stored: the sign-in page shows at once");
    r.ready();
    const form = r.document.querySelector("form");
    form.querySelector('input[name="token"]').value = TOKEN;
    const ev = r.submit(form);
    assert.ok(ev.defaultPrevented, "the sign-in form is posted by the script");
    assert.strictEqual(form.querySelector('input[name="token"]').value, "", "the token field is cleared");
    await settle();
    const [login, load] = r.events.fetches;
    assert.strictEqual(login.init.method, "POST");
    assert.strictEqual(login.init.credentials, "same-origin");
    // (JSON: the object comes from the script's realm, so its prototype differs.)
    assert.strictEqual(JSON.stringify(login.init.headers), JSON.stringify({ "X-Approved-Login": "1" }));
    assert.strictEqual(new URLSearchParams(login.init.body).get("token"), TOKEN);
    assert.strictEqual(r.store.get(KEY), SESSION, "kept in this tab's sessionStorage");
    assert.ok(!Array.from(r.local.values()).includes(SESSION), "never in localStorage");
    assert.strictEqual(load.url, BASE + "/connect");
    assert.strictEqual(load.init.headers["X-Approved-Session"], SESSION);
    assert.strictEqual(r.meta("session-flow"), "header");
    assert.strictEqual(r.meta("csrf-token"), "session-csrf-1");
    assert.strictEqual(r.document.title, "Connect · Approved");
    assert.ok(r.document.body.textContent.includes("shim"), "the connect page was swapped in");
    assert.deepStrictEqual(r.events.replaced, [BASE + "/connect#/connect"]);

    // In-console links become navigations to the sign-in document with a page fragment.
    const policyLink = r.document.querySelector('a[href="' + BASE + '/policy"]');
    assert.ok(r.click(policyLink).defaultPrevented);
    assert.deepStrictEqual(r.events.assigns, [BASE + "/login#/policy"]);
    const shim = r.document.querySelector('a[href="' + BASE + '/downloads/hermes-hook-shim.sh"]');
    assert.ok(!r.click(shim).defaultPrevented, "the public download is a plain navigation");
    assert.ok(!r.click(policyLink, { ctrlKey: true }).defaultPrevented, "modified clicks pass");

    // The connect form posts with the header and swaps in the answer.
    r.g.fetch = (url, init) => {
      r.events.fetches.push({ url: url, init: init });
      return Promise.resolve(new Response("PAGE:connect-bundle", { status: 200, headers: { "content-type": "text/html" } }));
    };
    const connect = r.document.querySelector('form[action="' + BASE + '/connect"]');
    assert.ok(r.submit(connect).defaultPrevented);
    await settle();
    const post = r.events.fetches[2];
    assert.strictEqual(post.init.method, "POST");
    assert.strictEqual(post.init.headers["X-Approved-Session"], SESSION);
    assert.strictEqual(new URLSearchParams(post.init.body).get("csrf"), "session-csrf-1");
    assert.ok(r.document.body.textContent.includes("connect-acme.sh bundle"));
  });

  await check("reload of a signed-in tab: hidden, fetched with the header, polled with it", async () => {
    const answers = [{ body: "PAGE:live-header" }, { body: "fragment-1" }, { status: 401, type: "application/json", body: "{}" }];
    const r = track(realm({ page: "login", stored: SESSION, hash: "#/", server: () => answers.shift() }));
    assert.strictEqual(r.pending(), true, "hidden until the signed-in page is in");
    r.ready();
    await settle();
    assert.strictEqual(r.pending(), false);
    assert.strictEqual(r.events.fetches[0].url, BASE + "/");
    assert.strictEqual(r.events.fetches[0].init.headers["X-Approved-Session"], SESSION);
    assert.deepStrictEqual(r.events.replaced, [BASE + "/#/"]);
    assert.strictEqual(r.events.intervals.length, 1, "console.js polls the live fragment");
    r.events.intervals[0]();
    await settle();
    assert.strictEqual(r.events.fetches[1].url, BASE + "/partials/live");
    assert.strictEqual(r.events.fetches[1].init.headers["X-Approved-Session"], SESSION);
    assert.strictEqual(r.document.getElementById("live").innerHTML, "fragment-1");
    const metrics = r.document.querySelector('a[href="' + BASE + '/metrics"]');
    assert.ok(r.click(metrics).defaultPrevented);
    assert.strictEqual(r.events.assigns[0], BASE + "/login#/metrics");
    const trace = r.document.querySelector('a[target="_blank"]');
    assert.ok(!r.click(trace).defaultPrevented, "external trace links are untouched");
    r.events.intervals[0](); // the session ends server-side
    await settle();
    assert.strictEqual(r.store.has(KEY), false, "a 401 forgets the session");
    assert.strictEqual(r.events.assigns[1], BASE + "/login#/");
    assert.deepStrictEqual(r.events.cleared, [1], "polling stops");
  });

  await check("metrics in header mode: JSON shown in the page", async () => {
    const r = track(realm({
      page: "login",
      stored: SESSION,
      hash: "#/metrics",
      server: () => json({ counters: { a: 1 }, open_requests: 2 })
    }));
    r.ready();
    await settle();
    assert.strictEqual(r.events.fetches[0].url, BASE + "/metrics");
    assert.ok(r.document.querySelector("main").textContent.includes('"open_requests": 2'));
    assert.strictEqual(r.meta("session-flow"), "header");
    assert.deepStrictEqual(r.events.replaced, [BASE + "/metrics#/metrics"]);
  });

  await check("public policy page in a signed-in tab: swapped, and policy.js loaded", async () => {
    const r = track(realm({ page: "policy-public", stored: SESSION, pathname: BASE + "/policy", server: () => ({ body: "PAGE:policy-header" }) }));
    assert.strictEqual(r.pending(), true);
    r.ready();
    await settle();
    assert.strictEqual(r.events.fetches[0].url, BASE + "/policy");
    assert.strictEqual(r.meta("csrf-token"), "session-csrf-1");
    assert.deepStrictEqual(r.events.appended, [], "policy.js was already on the page");
    assert.ok(r.document.querySelector('form[action="' + BASE + '/logout"]'), "signed-in variant");
  });

  await check("sign-in document swapped to the policy page loads policy.js once", async () => {
    const r = track(realm({ page: "login", stored: SESSION, hash: "#/policy", server: () => ({ body: "PAGE:policy-header" }) }));
    r.ready();
    await settle();
    assert.deepStrictEqual(r.events.appended, [BASE + "/static/policy.js"]);
  });

  await check("sign-out in header mode: POST with the header, then forget", async () => {
    const answers = [{ body: "PAGE:live-header" }, json({ detail: "signed out" })];
    const r = track(realm({ page: "login", stored: SESSION, server: () => answers.shift() }));
    r.ready();
    await settle();
    const out = r.document.querySelector('form[action="' + BASE + '/logout"]');
    assert.ok(r.submit(out).defaultPrevented);
    await settle();
    const post = r.events.fetches[1];
    assert.strictEqual(post.url, BASE + "/logout");
    assert.strictEqual(post.init.headers["X-Approved-Session"], SESSION);
    assert.strictEqual(new URLSearchParams(post.init.body).get("csrf"), "session-csrf-1");
    assert.strictEqual(r.store.has(KEY), false);
    assert.deepStrictEqual(r.events.assigns, [BASE + "/login"]);
  });

  await check("a dead stored session shows the sign-in form in place", async () => {
    const r = track(realm({ page: "login", stored: "1.dead", server: () => ({ status: 401, type: "application/json", body: "{}" }) }));
    r.ready();
    await settle();
    assert.strictEqual(r.store.has(KEY), false);
    assert.strictEqual(r.pending(), false);
    assert.deepStrictEqual(r.events.assigns, [], "no navigation loop");
    assert.ok(r.document.querySelector('input[name="token"]'));
  });

  await check("where cookies arrive: the cookie flow, nothing stored, nothing intercepted", async () => {
    const r = track(realm({ page: "login", hash: "#/connect", server: () => json({ flow: "cookie" }) }));
    r.ready();
    const form = r.document.querySelector("form");
    form.querySelector('input[name="token"]').value = TOKEN;
    r.submit(form);
    await settle();
    assert.strictEqual(r.store.size, 0);
    assert.deepStrictEqual(r.events.assigns, [BASE + "/connect"]);

    const c = track(realm({ page: "live-cookie", stored: SESSION, pathname: BASE + "/", server: () => { throw new Error("no fetch"); } }));
    assert.strictEqual(c.store.has(KEY), false, "a cookie-flow page drops a stale stored value");
    assert.strictEqual(c.pending(), false);
    c.ready();
    const link = c.document.querySelector('a[href="' + BASE + '/connect"]');
    assert.ok(!c.click(link).defaultPrevented);
    const out = c.document.querySelector('form[action="' + BASE + '/logout"]');
    assert.ok(!c.submit(out).defaultPrevented, "cookie-flow forms submit natively");
  });

  await check("sign-in errors: 401 shows the detail, 403 reloads for a fresh CSRF cookie", async () => {
    const answers = [json({ detail: "That token is not right." }, 401), json({ detail: "Session expired; try again." }, 403)];
    const r = track(realm({ page: "login", server: () => answers.shift() }));
    r.ready();
    const form = r.document.querySelector("form");
    form.querySelector('input[name="token"]').value = "wrong";
    r.submit(form);
    await settle();
    assert.strictEqual(r.document.querySelector(".error").textContent, "That token is not right.");
    assert.strictEqual(r.store.size, 0);
    r.submit(form);
    await settle();
    assert.strictEqual(r.events.reloads, 1);
  });

  await check("the session value and the token never reach a URL or another request", async () => {
    for (const r of allRealms) {
      const urls = []
        .concat(r.events.fetches.map((f) => f.url))
        .concat(r.events.assigns)
        .concat(r.events.replaced)
        .concat([r.location.pathname + r.location.hash]);
      for (const u of urls) {
        assert.ok(!u.includes(SESSION.split(".")[1]), "session value in URL " + u);
        assert.ok(!u.includes(TOKEN), "token in URL " + u);
      }
      for (const f of r.events.fetches) {
        const headers = f.init.headers || {};
        for (const name of Object.keys(headers)) {
          assert.ok(!/^(cookie|authorization)$/i.test(name), "sets " + name);
        }
        const body = f.init.body ? String(f.init.body) : "";
        if (f.url !== BASE + "/login") assert.ok(!body.includes(TOKEN), "token sent to " + f.url);
        if (f.url === BASE + "/login") assert.ok(!("X-Approved-Session" in headers), "no session on sign-in");
      }
      assert.ok(!Array.from(r.local.values()).some((v) => v.includes(SESSION)), "localStorage");
    }
  });

  console.log("session harness: all " + passed + " checks passed");
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
