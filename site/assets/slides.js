/*
 * The Approved demo deck: reveal.js 5.2.1 renders copy/slides.md with its Markdown plugin, then
 * this plugin (registered after it, so it runs on the converted slides and before reveal.js
 * indexes fragments) applies the deck grammar.
 *
 * Key lines sit at the top of a slide, one per line, in their own block (a blank line before
 * the heading):
 *
 *   class: title|big|split|chat|code|join|end
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
 *   ```chat             a Telegram thread (assets/chat.js: gate>, judge>, agent>, human>);
 *                       every bubble is a fragment
 *   ```lang             in a split or code slide, a code sticker in the right-hand column
 *   bullets             each item is a fragment
 *   **Title** — body    in the right-hand column of split/chat/code: a stat sticker; loud
 *                       (dashed amber) when the title starts "Known gap" or contains "FAIL"
 *   ![alt](x.svg)       an SVG image is a diagram: no keyline, shadow or backing
 *   {{PLACEHOLDER}}     anywhere in the text: a dashed gold chip
 *
 * Layouts: title/end put the approval.md wordmark on the right; split/chat/code put the heading
 * (and a lead-in paragraph) on the left and the rest on the right (code is split with the code
 * block as the right-hand sticker); join puts the QR sticker on the left (about 45%) and the
 * heading, paragraphs and bullets on the right; big is a shout (one heading, one paragraph)
 * sized for the back of the room.
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
    end: true
  };
  var COLUMNS = { split: true, chat: true, code: true, hero: true }; // heading left, content right
  var KEY_LINE = /^(class|badge|qr|qr-caption|fragments):[ \t]*(.+?)[ \t]*$/;
  var PH_RE = /\{\{\s*([A-Z0-9_]+)\s*\}\}/g;
  var PH_ONE = /\{\{\s*([A-Z0-9_]+)\s*\}\}/;
  var QR_SAFE = /^(https:\/\/|tg:\/\/|mailto:)\S+$/i;
  var QR_INK = "#17191d"; // Liquorice modules on white in both themes: scanners want contrast
  var QR_PAPER = "#ffffff";
  var QR_QUIET = 4; // quiet zone, in modules
  var SVG_NS = "http://www.w3.org/2000/svg";

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

    renderChats(section, fragments);

    if (fragments) {
      each(section.querySelectorAll("li"), function (li) {
        if (li.parentElement && li.parentElement.closest("li")) return; // nested: part of its parent

        if (!li.closest(".thread")) li.classList.add("fragment");
      });
    }

    markDiagrams(section);

    if (COLUMNS[layout]) {
      columns(section);
      statParagraphs(section, fragments);
    }
    if (layout === "grid") gridStats(section, fragments);
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
    keys.badge.forEach(function (text) {
      addBadge(section, text);
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
      }
    };
  };

  // Exposed for checking the renderer against a test string (no effect on the deck).
  window.ApprovedDeck.qrCard = qrCard;
})();
