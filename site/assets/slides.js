/*
 * The Approved demo deck: reveal.js 5.2.1 renders copy/slides.md with its Markdown plugin, then
 * this plugin (registered after it, so it runs on the converted slides and before reveal.js
 * indexes fragments) applies the deck grammar.
 *
 * Key lines sit at the top of a slide, one per line, in their own block (a blank line before
 * the heading):
 *
 *   class: title|big|split|chat|scene|code|hero|grid|ledger|join|end|stamp|meme|logos
 *                       the slide's layout (a class on <section>)
 *   badge: text         a sticker badge in the slide corner; anything but "live" is loud amber
 *   qr: value           a large QR code sticker. value is an https:, tg: or mailto: link, or a
 *                       {{PLACEHOLDER}}: a placeholder (or any value holding one, or a value
 *                       with another scheme) renders as a dashed gold chip and no QR code, so
 *                       an unfinished link can never be scanned. A qr: slide with no class:
 *                       line gets class: join; with another layout the sticker goes after the
 *                       content. Only the first qr: line counts.
 *   qr-caption: text    the line under the QR code, in the display face; the link itself is
 *                       printed under it in small monospace so people can type it. A join
 *                       slide with no qr: line shows a {{QR_LINK}} chip. Key lines are plain
 *                       text: markup such as **bold** in the block keeps it from being read.
 *   fragments: off|on   off: bullets, stat stickers and chat bubbles are all shown at once.
 *                       Default on, except on a slide with a qr: line (instructions people
 *                       must read together), where the default is off.
 *
 * And in the slide body:
 *
 *   ```chat             a Telegram thread (assets/chat.js: gate>, judge>, agent>, human>,
 *                       system>, buttons>, badge>, and the stamps held>, blocked>, stamp>,
 *                       verdict>); every message is a fragment
 *   ```lang             in a split or code slide, a code sticker in the right-hand column
 *   bullets             each item is a fragment
 *   **Title** — body    in the right-hand column of split/chat/scene/code: a stat sticker;
 *                       loud (dashed amber) when the title starts "Known gap" or contains
 *                       "FAIL". On split and grid a title that starts with a number
 *                       ("43,776", "1.0", "0 of 7", "2.1 s") counts up from 0 when shown
 *   ![alt](x.svg)       an SVG image is a diagram: no keyline, shadow or backing
 *   {{PLACEHOLDER}}     anywhere in the text: a dashed gold chip
 *
 * Layouts: title/end put the approval.md wordmark on the right; split/chat/code put the heading
 * (and a lead-in paragraph) on the left and the rest on the right (code is split with the code
 * block as the right-hand sticker); join puts the QR sticker on the left (about 45%) and the
 * heading, paragraphs and bullets on the right; big is a shout (one heading, one paragraph)
 * sized for the back of the room.
 *
 * More layouts:
 *   scene   chat, but the thread autoplays: on arrival its fragments reveal one every 900ms
 *           (reveal.js's nextFragment(), only while the slide is current). → still steps and
 *           the timer restarts from the new index; ← pauses it; leaving the slide stops it. A
 *           "▶ autoplay" chip (.autoplay-chip) sits in the top left corner and reads "▶ auto"
 *           (.is-playing) while the timer runs. Reduced motion: every bubble shown, no timer.
 *   stamp   a dark full-bleed slide; "# heading" is a rubber stamp that slams in, "## heading"
 *           and paragraphs fade in under it.
 *   meme    "# heading", then "## nope" + a list and "## yep" + a list: two sticker rows with a
 *           reaction figure (assets/nope.svg, assets/yep.svg) left; the "##" lines are not
 *           shown. Each row is a fragment; the yep row pops.
 *   logos   heading on top; every top-level bullet is a big pill chip that bounces in; a
 *           paragraph after the list sits under the chips.
 *
 * Motion (all of it off under prefers-reduced-motion and in ?print-pdf): the current slide gets
 * .is-shown, removed and re-added on every visit, so the CSS animations keyed on it replay (the
 * stamp slam, the big-slide paragraph and code-sticker wipes, still stamps in a thread). Stat
 * counters, the thread buzz when a held>/blocked> stamp shows, and the scene autoplay run on
 * reveal.js's slidechanged / fragmentshown events. assets/loop.svg draws its own arrows; its
 * <img> src gets a fresh "#t=" fragment on each show so the drawing replays.
 *
 * Key lines are stripped from the rendered slide. Everything from the Markdown reaches the DOM
 * as text or through reveal.js's own Markdown rendering; attribute values are whitelisted, and
 * the QR code is built with createElementNS, never from a markup string. Images resolve
 * against this script's own URL (assets/), so a deck page in a subfolder works too.
 */
(function () {
  "use strict";

  var LAYOUTS = {
    title: true,
    big: true,
    split: true,
    chat: true,
    code: true,
    hero: true,
    grid: true,
    ledger: true,
    join: true,
    end: true,
    stamp: true,
    scene: true,
    meme: true,
    logos: true
  };
  // heading left, content right
  var COLUMNS = { split: true, chat: true, scene: true, code: true, hero: true };
  var KEY_LINE = /^(class|badge|qr|qr-caption|fragments):[ \t]*(.+?)[ \t]*$/;
  var PH_RE = /\{\{\s*([A-Z0-9_]+)\s*\}\}/g;
  var PH_ONE = /\{\{\s*([A-Z0-9_]+)\s*\}\}/;
  var QR_SAFE = /^(https:\/\/|tg:\/\/|mailto:)\S+$/i;
  var QR_INK = "#17191d"; // Liquorice modules on white in both themes: scanners want contrast
  var QR_PAPER = "#ffffff";
  var QR_QUIET = 4; // quiet zone, in modules
  var SVG_NS = "http://www.w3.org/2000/svg";
  var SCENE_STEP_MS = 900; // a scene slide reveals its next message this often
  var COUNT_MS = 900; // a stat counter runs from 0 to its value in this long
  // a number at the start of a stat title: "43,776", "1.0", "0 of 7", "2.1 s"
  var COUNT_RE = /^(\s*)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?/;
  // SVG diagrams that animate themselves (a <style> inside the file): replayed on every show
    var REPLAY = /$^/; // nothing replays: diagrams are static and wiped in by CSS

  function reduceMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  /** assets/ as an absolute URL, taken from this script's src while it is executing. */
  var ASSETS = (function () {
    var script = document.currentScript;
    if (!script || !script.src) {
      var all = document.querySelectorAll('script[src$="slides.js"]');
      script = all[all.length - 1];
    }
    try {
      return new URL(".", script.src).href;
    } catch (e) {
      return "assets/";
    }
  })();

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function each(list, fn) {
    Array.prototype.forEach.call(list, fn);
  }

  /**
   * Pull key lines out of the slide's top-level paragraphs. A paragraph qualifies when it is
   * plain text, or text plus the links GFM makes from a bare URL (so "qr: https://…" works).
   */
  function takeKeys(section) {
    var keys = { class: [], badge: [], qr: [], "qr-caption": [], fragments: [] };
    Array.prototype.slice.call(section.querySelectorAll(":scope > p")).forEach(function (p) {
      var onlyLinks = Array.prototype.every.call(p.children, function (c) {
        return c.tagName === "A";
      });
      if (!onlyLinks) return;
      var lines = p.textContent.split(/\r?\n/);
      var rest = [];
      lines.forEach(function (line) {
        var m = line.trim().match(KEY_LINE);
        if (m) keys[m[1]].push(m[2]);
        else rest.push(line);
      });
      if (rest.join("").trim() === "") p.parentNode.removeChild(p);
      else if (rest.length !== lines.length) p.textContent = rest.join("\n");
    });
    return keys;
  }

  function placeholderChip(name) {
    var chip = el("mark", "ph", "{{" + name + "}}");
    chip.title = "Placeholder: replace " + name + " in site/copy";
    return chip;
  }

  /** Replace {{PLACEHOLDERS}} in text nodes with chips (not in notes or code). */
  function highlightPlaceholders(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var hits = [];
    var node;
    while ((node = walker.nextNode())) {
      var parent = node.parentElement;
      if (parent && parent.closest(".ph, aside, code, pre, svg")) continue;
      PH_RE.lastIndex = 0;
      if (PH_RE.test(node.nodeValue)) hits.push(node);
    }
    hits.forEach(function (textNode) {
      var frag = document.createDocumentFragment();
      var value = textNode.nodeValue;
      var last = 0;
      var m;
      PH_RE.lastIndex = 0;
      while ((m = PH_RE.exec(value))) {
        if (m.index > last) frag.appendChild(document.createTextNode(value.slice(last, m.index)));
        frag.appendChild(placeholderChip(m[1]));
        last = m.index + m[0].length;
      }
      if (last < value.length) frag.appendChild(document.createTextNode(value.slice(last)));
      textNode.parentNode.replaceChild(frag, textNode);
    });
  }

  function renderChats(section, fragments) {
    Array.prototype.slice
      .call(section.querySelectorAll("pre > code.chat, pre > code.language-chat"))
      .forEach(function (code) {
        var fig = window.ApprovedChat.render(code.textContent, {
          avatars: { gate: ASSETS + "icon.svg", judge: null },
          fragments: fragments,
          label: "Example Telegram conversation"
        });
        var pre = code.parentNode;
        pre.parentNode.replaceChild(fig, pre);
      });
  }

  function addBadge(section, text) {
    var loud = /simulated|hermetic|illustrative|local/i.test(text) || !/^live$/i.test(text.trim());
    var b = el("span", "badge slide-badge " + (loud ? "badge--loud" : "badge--live"), text);
    section.appendChild(b);
  }

  function isAside(node) {
    return node.tagName === "ASIDE" || node.classList.contains("slide-badge");
  }

  /** split / chat / code: heading (and any lead-in paragraph) left, everything else right. */
  function columns(section) {
    var a = el("div", "col-a");
    var b = el("div", "col-b");
    var kids = Array.prototype.slice.call(section.children);
    var inB = false;
    kids.forEach(function (node) {
      if (isAside(node)) return;
      var firstEl = node.firstElementChild;
      var startsWith = function (tag) {
        return firstEl && firstEl.tagName === tag && node.firstChild === firstEl;
      };
      // An image paragraph is content, never a lead-in: it belongs on the right.
      var leadIn =
        !inB &&
        (/^H[1-3]$/.test(node.tagName) ||
          (node.tagName === "P" && !startsWith("STRONG") && !startsWith("IMG")));
      if (leadIn) a.appendChild(node);
      else {
        inB = true;
        b.appendChild(node);
      }
    });
    section.insertBefore(b, section.firstChild);
    section.insertBefore(a, b);
  }

  /** join: the QR sticker left, everything else (heading, paragraphs, bullets) right. */
  function joinColumns(section, card) {
    var a = el("div", "col-qr");
    var b = el("div", "col-b");
    Array.prototype.slice.call(section.children).forEach(function (node) {
      if (!isAside(node)) b.appendChild(node);
    });
    a.appendChild(card);
    section.insertBefore(b, section.firstChild);
    section.insertBefore(a, b);
  }

  function statParagraphs(section, fragments) {
    each(section.querySelectorAll(".col-b > p"), function (p) {
      if (p.firstChild && p.firstChild.nodeName === "STRONG") {
        p.classList.add("stat");
        // what is not done must look not done: a known gap or a FAIL is a loud sticker
        var title = p.firstChild.textContent.trim();
        if (/^known gap/i.test(title) || /FAIL/.test(title)) p.classList.add("stat--loud");
        if (fragments) p.classList.add("fragment");
        // "**Title** — body": drop the dash, the title is already on its own line
        var after = p.firstChild.nextSibling;
        if (after && after.nodeType === 3) after.nodeValue = after.nodeValue.replace(/^\s*[—–-]\s*/, "");
      }
    });
  }

  /** title / end: the approval.md wordmark (1560×320) in the right-hand column. */
  /** grid: every "**Title** — body" paragraph is a stat tile in a two- or three-up grid. */
  function gridStats(section, fragments) {
    var wrap = el("div", "stat-grid");
    var stats = Array.prototype.filter.call(section.querySelectorAll(":scope > p"), function (p) {
      return p.firstChild && p.firstChild.nodeName === "STRONG";
    });
    if (!stats.length) return;
    stats.forEach(function (p) {
      p.classList.add("stat");
      if (fragments) p.classList.add("fragment");
      var after = p.firstChild.nextSibling;
      if (after && after.nodeType === 3) after.nodeValue = after.nodeValue.replace(/^\s*[—–-]\s*/, "");
      wrap.appendChild(p);
    });
    var notes = section.querySelector(":scope > aside");
    section.insertBefore(wrap, notes);
  }

  /** ledger: each "## heading" and the list under it become one column of three. */
  function ledgerColumns(section) {
    var wrap = el("div", "ledger");
    var col = null;
    Array.prototype.slice.call(section.children).forEach(function (node) {
      if (node.tagName === "H2") {
        col = el("div", "ledger-col");
        wrap.appendChild(col);
        col.appendChild(node);
      } else if (col && (node.tagName === "UL" || node.tagName === "P")) {
        col.appendChild(node);
      }
    });
    if (!wrap.children.length) return;
    var notes = section.querySelector(":scope > aside");
    section.insertBefore(wrap, notes);
  }

  /**
   * meme: the two-panel format. "## nope" + a list and "## yep" + a list become two sticker
   * rows, each a reaction figure (assets/nope.svg, assets/yep.svg) left and the list right. The
   * two headings are not rendered (each becomes its figure's alt text); each row is a fragment.
   */
  function memeRows(section, fragments) {
    var grid = el("div", "meme-grid");
    var row = null;
    var n = 0;
    Array.prototype.slice.call(section.children).forEach(function (node) {
      if (node.tagName === "H2") {
        row = null;
        if (n >= 2) return; // a third "##" stays where it is
        var kind = n === 0 ? "nope" : "yep";
        n += 1;
        row = el("div", "meme-row meme-row--" + kind);
        if (fragments) row.classList.add("fragment");
        var fig = el("img", "meme-fig");
        fig.src = ASSETS + kind + ".svg";
        fig.alt = node.textContent.trim() || kind;
        fig.width = 190;
        fig.height = 190;
        row.appendChild(fig);
        row.appendChild(el("div", "meme-list"));
        grid.appendChild(row);
        node.parentNode.removeChild(node);
      } else if (row && !isAside(node)) {
        row.lastChild.appendChild(node);
      }
    });
    if (!grid.children.length) return;
    section.insertBefore(grid, section.querySelector(":scope > aside"));
  }

  /** logos: every top-level bullet is a big pill chip in a centred, wrapping row. */
  function logoChips(section) {
    each(section.querySelectorAll(":scope > ul"), function (ul) {
      ul.classList.add("logo-row");
      each(ul.children, function (li) {
        li.classList.add("logo-chip");
      });
    });
  }

  /**
   * Stat counters: a stat title that starts with a number gets that number wrapped in
   * <span class="count" data-count-to data-count-decimals [data-count-commas] data-count-text>.
   * The span holds the final text, so without JS, in print and with reduced motion the value is
   * simply there; countUp() runs it from 0 when it is shown.
   */
  function markCounters(section) {
    each(section.querySelectorAll(".stat > strong:first-child"), function (strong) {
      var t = strong.firstChild;
      if (!t || t.nodeType !== 3) return;
      var m = t.nodeValue.match(COUNT_RE);
      if (!m) return;
      var text = m[2] + (m[3] || "");
      var span = el("span", "count", text);
      span.setAttribute("data-count-to", m[2].replace(/,/g, "") + (m[3] || ""));
      span.setAttribute("data-count-decimals", String(m[3] ? m[3].length - 1 : 0));
      if (m[2].indexOf(",") !== -1) span.setAttribute("data-count-commas", "");
      span.setAttribute("data-count-text", text);
      var frag = document.createDocumentFragment();
      if (m[1]) frag.appendChild(document.createTextNode(m[1]));
      frag.appendChild(span);
      var rest = t.nodeValue.slice(m[0].length);
      if (rest) frag.appendChild(document.createTextNode(rest));
      strong.replaceChild(frag, t);
    });
  }

  function formatCount(value, decimals, commas) {
    if (commas) {
      return value.toLocaleString("en-US", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
      });
    }
    return value.toFixed(decimals);
  }

  var countFrames = typeof WeakMap === "function" ? new WeakMap() : null;

  /** Stop a running counter and show its final text. */
  function settleCount(node) {
    if (countFrames && countFrames.has(node)) {
      cancelAnimationFrame(countFrames.get(node));
      countFrames.delete(node);
    }
    node.textContent = node.getAttribute("data-count-text");
  }

  /** Run a counter from 0 to its value over COUNT_MS, ease-out, keeping commas and decimals. */
  function countUp(node) {
    settleCount(node);
    var to = parseFloat(node.getAttribute("data-count-to"));
    if (!countFrames || !(to > 0) || typeof requestAnimationFrame !== "function") return;
    var decimals = parseInt(node.getAttribute("data-count-decimals"), 10) || 0;
    var commas = node.hasAttribute("data-count-commas");
    var start = null;
    node.textContent = formatCount(0, decimals, commas);
    var frame = function (now) {
      if (start === null) start = now;
      var t = Math.min(1, (now - start) / COUNT_MS);
      if (t >= 1) {
        settleCount(node);
        return;
      }
      node.textContent = formatCount(to * (1 - Math.pow(1 - t, 3)), decimals, commas);
      countFrames.set(node, requestAnimationFrame(frame));
    };
    countFrames.set(node, requestAnimationFrame(frame));
  }

  function mascot(section) {
    var img = el("img", "deck-mascot");
    img.src = ASSETS + "wordmark.svg";
    img.alt = "approval.md";
    img.width = 420;
    img.height = 86;
    section.appendChild(img);
  }

  /** An SVG image in the copy is a hand-drawn diagram, not a screenshot. */
  function markDiagrams(section) {
    each(section.querySelectorAll("img"), function (img) {
      if (img.classList.contains("meme-fig")) return; // drawn stickers with their own sizing
      if (/\.svg([?#].*)?$/i.test(img.getAttribute("src") || "")) img.classList.add("diagram");
    });
  }

  /** The QR code as an SVG element (one path, horizontal runs merged), or null. */
  function qrSvg(text) {
    var lib = window.qrcode;
    if (typeof lib !== "function") return null;
    try {
      if (lib.stringToBytesFuncs && lib.stringToBytesFuncs["UTF-8"]) {
        lib.stringToBytes = lib.stringToBytesFuncs["UTF-8"];
      }
      var qr = lib(0, "M");
      qr.addData(text);
      qr.make();
      var n = qr.getModuleCount();
      var size = n + QR_QUIET * 2;
      var d = [];
      for (var r = 0; r < n; r += 1) {
        var c = 0;
        while (c < n) {
          if (!qr.isDark(r, c)) {
            c += 1;
            continue;
          }
          var start = c;
          while (c < n && qr.isDark(r, c)) c += 1;
          var len = c - start;
          d.push("M" + (start + QR_QUIET) + " " + (r + QR_QUIET) + "h" + len + "v1h-" + len + "z");
        }
      }
      var svg = document.createElementNS(SVG_NS, "svg");
      svg.setAttribute("viewBox", "0 0 " + size + " " + size);
      svg.setAttribute("shape-rendering", "crispEdges");
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", "QR code for " + text);
      var bg = document.createElementNS(SVG_NS, "rect");
      bg.setAttribute("width", String(size));
      bg.setAttribute("height", String(size));
      bg.setAttribute("fill", QR_PAPER);
      var path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d.join(""));
      path.setAttribute("fill", QR_INK);
      svg.appendChild(bg);
      svg.appendChild(path);
      return svg;
    } catch (e) {
      return null; // too long, or the library misbehaved: fail to a chip, never a broken code
    }
  }

  /** qr: value (+ qr-caption:) -> <figure class="qr-card sticker">. */
  function qrCard(value, caption) {
    var fig = el("figure", "qr-card sticker");
    var box = el("div", "qr-code");
    var ph = value.match(PH_ONE);
    var scannable = false;
    if (ph) {
      box.appendChild(placeholderChip(ph[1]));
    } else if (!QR_SAFE.test(value)) {
      box.appendChild(placeholderChip(value || "LINK")); // unknown scheme: fail closed
    } else {
      var svg = qrSvg(value);
      if (svg) {
        box.appendChild(svg);
        scannable = true;
      } else {
        box.appendChild(el("mark", "ph", "QR code unavailable: type the link"));
      }
    }
    if (!scannable) box.classList.add("qr-code--ph");
    fig.appendChild(box);

    var showUrl = !ph && QR_SAFE.test(value);
    if (caption || showUrl) {
      var cap = el("figcaption", "qr-caption");
      if (caption) cap.appendChild(el("span", "qr-caption-text", caption));
      if (showUrl) cap.appendChild(el("code", "qr-url", value));
      highlightPlaceholders(cap);
      fig.appendChild(cap);
    }
    fig.setAttribute("data-qr", scannable ? "code" : "placeholder");
    return fig;
  }

  function fragmentsSetting(keys) {
    var v = keys.fragments.length ? keys.fragments[keys.fragments.length - 1].toLowerCase() : "";
    if (v === "off") return false;
    if (v === "on") return true;
    return !keys.qr.length; // a qr: slide shows everything at once unless told otherwise
  }

  function processSlide(section) {
    var keys = takeKeys(section);
    var layout = keys.class
      .map(function (c) {
        return c.toLowerCase();
      })
      .filter(function (c) {
        return LAYOUTS[c];
      })[0];
    if (!layout && keys.qr.length) layout = "join";
    if (layout) section.classList.add(layout);
    var fragments = fragmentsSetting(keys);
    // a scene autoplays its thread; with reduced motion every bubble is simply there
    if (layout === "scene" && reduceMotion()) fragments = false;

    renderChats(section, fragments);

    if (layout === "meme") memeRows(section, fragments);
    if (layout === "logos") logoChips(section);

    if (fragments) {
      each(section.querySelectorAll("li"), function (li) {
        if (li.parentElement && li.parentElement.closest("li")) return; // nested: part of its parent
        if (li.closest(".meme-row")) return; // the meme row is the fragment, not its items

        if (!li.closest(".thread")) li.classList.add("fragment");
      });
    }

    markDiagrams(section);

    if (COLUMNS[layout]) {
      columns(section);
      statParagraphs(section, fragments);
    }
    if (layout === "grid") gridStats(section, fragments);
    if (layout === "grid" || layout === "split") markCounters(section);
    if (layout === "ledger") ledgerColumns(section);
    if (layout === "big") {
      var shout = section.querySelector(":scope > h1, :scope > h2");
      if (shout && shout.textContent.trim().length > 48) shout.classList.add("shout--long");
    }

    highlightPlaceholders(section);

    if (keys.qr.length) {
      var card = qrCard(keys.qr[0], keys["qr-caption"][0] || "");
      if (layout === "join") joinColumns(section, card);
      else {
        var notes = section.querySelector(":scope > aside");
        section.insertBefore(card, notes);
      }
    } else if (layout === "join") {
      joinColumns(section, qrCard("{{QR_LINK}}", keys["qr-caption"][0] || ""));
    }

    if (layout === "title" || layout === "end") mascot(section);
    if (layout === "scene" && fragments && section.querySelector(".thread .fragment")) {
      var chip = el("span", "autoplay-chip", "▶ autoplay");
      chip.setAttribute("aria-hidden", "true");
      section.appendChild(chip);
      section.setAttribute("data-autoplay", "");
    }
    keys.badge.forEach(function (text) {
      addBadge(section, text);
    });
  }

  /**
   * Motion, driven by reveal.js events. Every slide that becomes current gets .is-shown
   * (removed and re-added, so CSS animations keyed on it replay on each visit: the stamp slam,
   * the typewriter wipes, still stamps in a thread). Counters, the thread buzz, self-animating
   * SVG diagrams and the scene autoplay are started here. Nothing runs in the ?print-pdf view,
   * and with reduced motion only .is-shown is toggled (the CSS ignores it).
   */
  function motion(reveal) {
    var root = reveal.getRevealElement();
    var print =
      (typeof reveal.isPrintView === "function" && reveal.isPrintView()) ||
      /[?&]print-pdf\b/.test(window.location.search);
    var still = function () {
      return print || reduceMotion();
    };
    var auto = { section: null, timer: 0, driving: false };

    function remaining() {
      var f = reveal.availableFragments();
      return !!(f && f.next);
    }

    function chip(section, playing) {
      var c = section && section.querySelector(":scope > .autoplay-chip");
      if (!c) return;
      c.textContent = playing ? "▶ auto" : "▶ autoplay";
      c.classList.toggle("is-playing", playing);
    }

    function stopAuto() {
      clearTimeout(auto.timer);
      auto.timer = 0;
      chip(auto.section, false);
      auto.section = null;
    }

    function scheduleAuto(section) {
      clearTimeout(auto.timer);
      auto.section = section;
      chip(section, true);
      auto.timer = setTimeout(stepAuto, SCENE_STEP_MS);
    }

    /** One autoplay step: only while this scene is still current and fragments remain. */
    function stepAuto() {
      auto.timer = 0;
      var section = auto.section;
      if (!section || reveal.getCurrentSlide() !== section || !remaining()) return stopAuto();
      if (typeof reveal.isPaused === "function" && reveal.isPaused()) return scheduleAuto(section);
      auto.driving = true;
      try {
        reveal.nextFragment();
      } finally {
        auto.driving = false;
      }
      if (reveal.getCurrentSlide() === section && remaining()) scheduleAuto(section);
      else stopAuto();
    }

    function maybeAuto(section) {
      if (still() || !section || !section.hasAttribute("data-autoplay")) return;
      if (reveal.getCurrentSlide() === section && remaining()) scheduleAuto(section);
    }

    function buzz(thread) {
      if (!thread || still()) return;
      thread.classList.remove("is-buzzing");
      void thread.offsetWidth; // restart the animation
      thread.classList.add("is-buzzing");
    }

    function replayDiagrams(section) {
      each(section.querySelectorAll("img.diagram"), function (img) {
        var base = (img.getAttribute("src") || "").split("#")[0];
        if (!REPLAY.test(base.split("?")[0])) return;
        img.setAttribute("src", base + "#t=" + Date.now());
      });
    }

    function onShow(current) {
      stopAuto();
      each(root.querySelectorAll(".slides section.is-shown"), function (s) {
        if (s === current) return;
        s.classList.remove("is-shown");
        each(s.querySelectorAll(".count"), settleCount);
      });
      if (!current) return;
      current.classList.remove("is-shown");
      void current.offsetWidth; // restart the CSS animations keyed on .is-shown
      current.classList.add("is-shown");
      if (still()) return;
      replayDiagrams(current);
      each(current.querySelectorAll(".count"), function (c) {
        if (!c.closest(".fragment")) countUp(c);
      });
      each(current.querySelectorAll(".thread"), function (t) {
        if (t.querySelector(".msg--buzz:not(.fragment)")) buzz(t);
      });
      // after reveal.js has finished this navigation (it may still sync fragments in this task)
      setTimeout(function () {
        maybeAuto(current);
      }, 0);
    }

    reveal.on("ready", function (e) {
      onShow(e.currentSlide || reveal.getCurrentSlide());
    });
    reveal.on("slidechanged", function (e) {
      onShow(e.currentSlide);
    });
    reveal.on("fragmentshown", function (e) {
      var shown = e.fragments || (e.fragment ? [e.fragment] : []);
      if (!still()) {
        each(shown, function (f) {
          if (f.classList.contains("msg--buzz")) buzz(f.closest(".thread"));
          if (f.classList.contains("count")) countUp(f);
          each(f.querySelectorAll(".count"), countUp);
        });
      }
      // a fragment the presenter showed (→): carry on autoplaying from the new index
      if (!auto.driving) {
        stopAuto();
        maybeAuto(reveal.getCurrentSlide());
      }
    });
    reveal.on("fragmenthidden", function (e) {
      var hidden = e.fragments || (e.fragment ? [e.fragment] : []);
      each(hidden, function (f) {
        each(f.querySelectorAll(".count"), settleCount);
      });
      if (!auto.driving) stopAuto(); // stepping back (←) pauses the autoplay; → resumes it
    });
    root.addEventListener("animationend", function (e) {
      if (e.animationName === "buzz" && e.target.classList) e.target.classList.remove("is-buzzing");
    });
  }

  window.ApprovedDeck = function () {
    return {
      id: "approveddeck",
      init: function (reveal) {
        var slides = reveal.getRevealElement().querySelectorAll(".slides section");
        each(slides, function (section) {
          if (section.querySelector("section")) return; // a vertical stack wrapper
          processSlide(section);
        });
        motion(reveal);
      }
    };
  };

  // Exposed for checking the renderer against a test string (no effect on the deck).
  window.ApprovedDeck.qrCard = qrCard;
})();
