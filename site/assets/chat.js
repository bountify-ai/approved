/*
 * Approved chat threads: turns a ```chat block from site/copy/slides.md into a Telegram-style
 * thread. On slides.html each bubble is a reveal.js fragment; animateIn() is kept for any page
 * that wants bubbles to arrive as a thread scrolls into view.
 *
 * Line grammar, one message per line:
 *   gate> text       the approval bot (left-hand side, approval.md's icon as the avatar,
 *                    labelled "Approval bot"): the only bot whose buttons grant
 *   judge> text      the judge bot (left-hand side, a scales glyph in a round sticker,
 *                    labelled "Judge bot"): advisory text, never buttons that grant
 *   agent> text      a person-side bubble (right-hand side), labelled "agent"
 *   human> text      a person-side bubble (right-hand side), labelled "you"
 *   name> text       any other name: a person-side bubble labelled with the name as written
 *   system> text     a centred service pill
 *   buttons> A | B   an inline keyboard row under the previous bot bubble
 *   badge> live|example|simulated|hermetic|illustrative|local|...
 *                    a sticker on the thread. "live" is quiet check green with a pulse,
 *                    "example" is quiet latte; anything else is loud (amber, striped), because
 *                    a simulated result must visibly say simulated.
 *
 * A line without a speaker continues the previous message. A bubble starting with "/" or "$" is
 * set as a command; one wrapped in "( … )" is a stage direction. Consecutive bubbles from the
 * same speaker form a run (name on the first, avatar on the last); a judge bubble after a gate
 * bubble starts a new run.
 *
 * Every string lands in the DOM through textContent, never innerHTML, so "<chat_id>" shows
 * literally and nothing in the copy can inject markup.
 *
 * The default gate avatar resolves against this script's own URL (assets/), not the page's, so
 * a page in a subfolder still finds it.
 */
(function () {
  "use strict";

  /** assets/ as an absolute URL, taken from this script's src while it is executing. */
  var ASSETS = (function () {
    var script = document.currentScript;
    if (!script || !script.src) {
      var all = document.querySelectorAll('script[src$="chat.js"]');
      script = all[all.length - 1];
    }
    try {
      return new URL(".", script.src).href;
    } catch (e) {
      return "assets/";
    }
  })();

  var BOTS = { gate: "Approval bot", judge: "Judge bot" };
  var PEOPLE = { human: "you" };
  var JUDGE_GLYPH = "⚖️"; // scales of justice

  function parse(text) {
    var out = { badges: [], items: [] };
    String(text || "")
      .split(/\r?\n/)
      .forEach(function (raw) {
        var line = raw.trim();
        if (!line) return;
        var m = line.match(/^([a-z][a-z0-9_-]*)>\s?(.*)$/i);
        if (!m) {
          // A line without a speaker continues the previous message.
          var last = out.items[out.items.length - 1];
          if (last && last.kind !== "buttons") last.text += "\n" + line;
          return;
        }
        var name = m[1];
        var who = name.toLowerCase();
        var body = m[2].trim();
        if (who === "badge") {
          if (body) out.badges.push(body);
        } else if (who === "buttons") {
          var labels = body
            .split("|")
            .map(function (s) {
              return s.trim();
            })
            .filter(Boolean);
          if (labels.length) out.items.push({ kind: "buttons", labels: labels });
        } else if (who === "system") {
          out.items.push({ kind: "system", text: body });
        } else if (Object.prototype.hasOwnProperty.call(BOTS, who)) {
          out.items.push({ kind: "bot", bot: who, who: BOTS[who], text: body });
        } else {
          // agent, human, and any other name: the person side, labelled with the name as
          // written ("human" reads as "you")
          var label = Object.prototype.hasOwnProperty.call(PEOPLE, who) ? PEOPLE[who] : name;
          out.items.push({ kind: "person", key: who, who: label, text: body });
        }
      });
    return out;
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function badgeClass(value) {
    var v = value.toLowerCase();
    // Fail loud: only "live" (a real capture) and "example" (a depiction that claims
    // nothing) are quiet. Unknown words get the amber treatment.
    if (v === "live") return "badge badge--live";
    if (v === "example") return "badge badge--example";
    return "badge badge--loud";
  }

  function textNode(text) {
    var p = el("p", "bubble-text", text);
    if (/^[/$]/.test(text)) p.className += " is-cmd";
    else if (/^\(.*\)$/.test(text)) p.className += " is-stage";
    return p;
  }

  /** The avatar for a bot bubble: an image when a URL is given, else a glyph sticker. */
  function avatarFor(bot, avatars) {
    var url = Object.prototype.hasOwnProperty.call(avatars, bot)
      ? avatars[bot]
      : bot === "gate"
        ? ASSETS + "icon.svg"
        : null;
    if (typeof url === "string" && url) {
      var img = el("img", "avatar avatar--" + bot);
      img.src = url;
      img.alt = "";
      img.width = 34;
      img.height = 34;
      img.decoding = "async";
      return img;
    }
    var glyph = el("span", "avatar avatar--" + bot, bot === "judge" ? JUDGE_GLYPH : "");
    glyph.setAttribute("aria-hidden", "true");
    return glyph;
  }

  /**
   * render(text, options) -> <figure class="thread sticker">
   * options.avatars    { gate: url, judge: url|null }. gate defaults to icon.svg next to this
   *                    script; a missing or null judge avatar renders the scales glyph.
   * options.fragments  true on slides: every message becomes a reveal.js fragment
   * options.label      accessible label for the thread
   */
  function render(text, options) {
    var opts = options || {};
    var avatars = opts.avatars || {};
    var data = parse(text);
    var fig = el("figure", "thread sticker");
    fig.setAttribute("role", "group");
    fig.setAttribute("aria-label", opts.label || "Example Telegram conversation");

    var loud = false;
    data.badges.forEach(function (b) {
      var cls = badgeClass(b);
      if (cls.indexOf("loud") !== -1) loud = true;
      var badge = el("span", cls, b);
      fig.appendChild(badge);
    });
    // Two badges on one thread: stack the second under the first.
    Array.prototype.forEach.call(fig.querySelectorAll(".badge"), function (b, i) {
      if (i > 0) b.style.top = 22 + i * 26 + "px";
    });
    if (loud) fig.className += " thread--loud";
    fig.dataset.badge = data.badges.join(" ").toLowerCase();

    var list = el("ol", "thread-list");
    var lastMsg = null;
    var lastBotCol = null;
    var index = 0;

    data.items.forEach(function (item) {
      if (item.kind === "buttons") {
        var row = el("div", "kb");
        row.setAttribute("role", "group");
        row.setAttribute("aria-label", "Inline buttons");
        item.labels.forEach(function (label) {
          row.appendChild(el("span", "kb-btn", label));
        });
        if (lastBotCol) lastBotCol.appendChild(row);
        else {
          var li0 = el("li", "msg msg--bot");
          li0.appendChild(row);
          list.appendChild(li0);
        }
        return;
      }

      var li = el("li", "msg msg--" + item.kind);
      if (item.kind === "bot") li.className += " msg--" + item.bot;
      li.style.setProperty("--i", String(index++));
      if (opts.fragments) li.className += " fragment";

      if (item.kind === "system") {
        li.appendChild(el("span", "pill", item.text));
        lastBotCol = null;
        lastMsg = null;
      } else {
        // Runs are per speaker: gate, judge and each person name are separate speakers.
        var speaker = item.kind === "bot" ? "bot:" + item.bot : "person:" + item.key;
        var sameAsBefore = lastMsg && lastMsg.speaker === speaker;
        if (sameAsBefore) li.className += " msg--cont";
        // Telegram shows the name on the first bubble of a run and the avatar on the last.
        if (sameAsBefore && lastMsg.node) lastMsg.node.className += " msg--run";

        var bubble = el("div", "bubble");
        bubble.appendChild(el("span", "who", item.who));
        bubble.appendChild(textNode(item.text));

        if (item.kind === "bot") {
          var col = el("div", "bubble-col");
          col.appendChild(bubble);
          li.appendChild(avatarFor(item.bot, avatars));
          li.appendChild(col);
          lastBotCol = col;
        } else {
          li.appendChild(bubble);
          lastBotCol = null;
        }
        lastMsg = { speaker: speaker, node: li };
      }
      list.appendChild(li);
    });

    fig.appendChild(list);
    return fig;
  }

  var reduceMotion = function () {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  };

  /** Bubbles arrive one after another when a thread scrolls into view (unused by the deck). */
  function animateIn(root) {
    var threads = Array.prototype.slice.call((root || document).querySelectorAll(".thread"));
    if (reduceMotion() || !("IntersectionObserver" in window)) return;
    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-in");
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.25, rootMargin: "0px 0px -8% 0px" }
    );
    threads.forEach(function (t) {
      t.classList.add("thread--pending");
      io.observe(t);
    });
  }

  window.ApprovedChat = { parse: parse, render: render, animateIn: animateIn, assets: ASSETS };
})();
