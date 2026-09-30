"use strict";
// Approved console: theme toggle, live-view polling, copy buttons. No framework.
// Page setup runs through ApprovedSession.onPage (static/session.js), so it runs again when
// that script swaps in a page fetched with the session header.

(function () {
  var session = window.ApprovedSession;
  var onPage = session ? session.onPage : function (fn) { fn(); };
  var timer = null;

  onPage(function theme() {
    var root = document.documentElement;
    try {
      var saved = localStorage.getItem("approved-theme");
      if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
    } catch (e) { /* storage unavailable: follow the system */ }
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var current = root.getAttribute("data-theme") ||
        (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
      var next = current === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("approved-theme", next); } catch (e) { /* ignore */ }
    });
  });

  onPage(function live() {
    // The server renders (and escapes) the fragment; this only swaps it in.
    if (timer !== null) { clearInterval(timer); timer = null; }
    var region = document.getElementById("live");
    if (!region) return;
    var every = Number(region.getAttribute("data-refresh-ms")) || 3000;
    var stamp = document.getElementById("live-updated");
    var base = (document.querySelector('meta[name="base-path"]') || {}).content || "";
    function tick() {
      fetch(base + "/partials/live", {
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error", // never replay the session header along a redirect
        headers: session ? session.headers({}) : {}
      })
        .then(function (r) {
          if (r.status === 401) {
            if (timer !== null) { clearInterval(timer); timer = null; }
            if (session) session.expired(); else window.location.assign(base + "/login");
            return null;
          }
          return r.ok ? r.text() : null;
        })
        .then(function (html) {
          if (html === null) return;
          region.innerHTML = html;
          if (stamp) stamp.textContent = "updated " + new Date().toLocaleTimeString();
        })
        .catch(function () { if (stamp) stamp.textContent = "console unreachable, retrying"; });
    }
    timer = setInterval(tick, every);
  });

  // One delegated listener for the whole tab: it survives page swaps, so it is not per page.
  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-copy]");
    if (!btn) return;
    var target = document.getElementById(btn.getAttribute("data-copy"));
    if (!target || !navigator.clipboard) return;
    navigator.clipboard.writeText(target.textContent).then(function () {
      var label = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = label; }, 1200);
    });
  });
})();
