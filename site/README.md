# Approved: the demo deck

The demo deck for the Approved pitch. What Approved is and how it works is in the
[repo README](../README.md); this folder is only the slides.

One static page, no build step. All wording lives in `copy/slides.md`; the HTML and JavaScript
only arrange it.

| File | What it is |
| --- | --- |
| `slides.html` | The demo deck (reveal.js 5.2.1). Loads `copy/slides.md`. |
| `copy/slides.md` | **The only copy.** Edit it, refresh, done. |
| `assets/brand.css` | Tokens (light "Linen" and dark "Ink", both from approval.md), stickers, chat threads. |
| `assets/slides.css` | The reveal.js structural rules the deck needs, plus the deck layouts. |
| `assets/chat.js` | Renders ```chat blocks as Telegram-style threads (approval bot, judge bot, people). |
| `assets/slides.js` | The deck plugin: key lines (`class:`, `badge:`, `qr:`, `qr-caption:`, `fragments:`), chat threads, code and stat stickers, fragments. |
| `assets/wordmark.svg`, `icon.svg` | approval.md's wordmark (title and end slides) and icon (favicon, approval bot avatar), copied from the approval.md brand folder. |
| `assets/arch.svg` | The architecture diagram: agent machine, tenant daemon, judge service, phone. |
| `vercel.json` | Static hosting: clean URLs (`/slides`) and security headers. |

## Preview

From the repo root:

```sh
python3 -m http.server -d site 8765
# http://localhost:8765/slides.html
```

Open the deck through a server, not `file://`: it fetches its Markdown. `chat.js` and
`slides.js` resolve images against their own URL, so a deck page in a subfolder only needs its
own `../` links (stylesheets, scripts, the Markdown path).

## Editing the deck (`copy/slides.md`)

- `---` on its own line starts a new slide. `Note:` starts the speaker notes (press **S** for
  the speaker view).
- Key lines sit at the top of a slide, one per line, plain text, with a blank line before the
  heading:
  - `class: title|big|split|chat|code|join|end` picks a layout: `title`/`end` put the
    approval.md wordmark on the right; `split`, `chat` and `code` put the heading (and a
    lead-in paragraph) on the left and the content on the right; `code` is `split` whose
    right-hand content is a fenced code block, set as a monospace sticker (a code block in any
    `split` slide gets the same sticker); `join` puts a QR sticker on the left (about 45%) and
    the heading, paragraphs and bullets on the right; `big` is one heading and one paragraph,
    sized for the back of the room (`#` or `##`).
  - `badge: text` puts a sticker in the slide's top-right corner. Anything other than `live` is
    loud amber, so `badge: simulated`, `badge: local` and `badge: n=12 · synthetic` cannot be
    missed.
  - `qr: <link>` draws a large QR code sticker (ink on white, 400px on the 1280×720 slide).
    The link must be `https://…`, `tg://…` or `mailto:…`. A `{{PLACEHOLDER}}` (or a link
    containing one, or any other scheme) renders a dashed gold chip instead and no QR code, so
    an unfinished link can never be scanned. A `qr:` slide without a `class:` line becomes
    `join`; a `join` slide without a `qr:` line shows a `{{QR_LINK}}` chip.
  - `qr-caption: text` is the line under the code, in the display face. The link itself is
    printed under the caption in small monospace so people can type it.
  - `fragments: off` shows everything on the slide at once (bullets, stat stickers, chat
    bubbles). A `qr:` slide defaults to off, since people need to read all the steps together;
    `fragments: on` brings the one-by-one reveal back.
- Otherwise bullets and chat bubbles arrive one by one as fragments (→ or space to advance).
- `{{PLACEHOLDERS}}` anywhere in a slide render as dashed gold chips, so nothing unfinished can
  be mistaken for finished.
- Paragraphs that start with `**Bold** —` in the right-hand column of a `split`, `chat` or
  `code` slide become stat stickers. A stat whose bold title starts with "Known gap" or contains
  "FAIL" is a loud sticker (dashed amber border), because what is not done must look not done.
- An `.svg` image (`![Architecture](assets/arch.svg)`) is a diagram: no keyline, shadow or
  backing, at most 520px tall. Other images are screenshot stickers.
- `?theme=dark` gives the dark deck; the default is light for projectors.
- `?print-pdf` is reveal.js's print layout (one slide per page, every fragment shown), to be
  printed from a desktop Chrome window (Save as PDF, margins none, background graphics on).
  It has not been verified for this deck: the Claude browser pane and headless Chrome both
  left it unpaginated, and slopbot.fyi/slides behaves the same there, so check it in a
  normal Chrome window before relying on a PDF.

## Chat grammar

````md
```chat
badge> simulated
agent> $ git push origin main
gate> Approval requested: vcs.push.main
buttons> Approve | Reject
judge> Judge (advisory AI, not an approval): NEEDS_HUMAN
human> (taps Reject)
```
````

| Line | Renders as |
| --- | --- |
| `gate> …` | The approval bot's bubble, on the left, with approval.md's icon as the avatar, labelled "Approval bot". |
| `judge> …` | The judge bot's bubble, on the left, with a ⚖️ glyph in a round sticker, labelled "Judge bot" (never in the Approve green). |
| `agent> …`, `human> …`, any other name | A bubble on the right, labelled with the name as written; `human` reads as "you". |
| `system> …` | A centred service pill. |
| `buttons> A \| B` | An inline keyboard row under the previous bot bubble. |
| `badge> live` | A quiet check-green "live" sticker with a pulse. |
| `badge> example` | A quiet latte sticker: a depiction that claims nothing. |
| `badge> simulated`, `hermetic`, `illustrative`, `local` (or any other word) | A loud amber, striped sticker, plus hazard tape and an amber shadow on the thread. Anything not live must visibly say so. |

Consecutive bubbles from one speaker form a run (name on the first, avatar on the last); a
judge bubble after a gate bubble starts a new run. A bubble starting with `/` or `$` is set as a
command; one wrapped in `( … )` is a stage direction (italic). Every character is escaped:
`<chat_id>` shows literally and nothing in the copy can inject markup.

## Rules the copy must keep

`service/tests/test_site_copy.py` checks `copy/slides.md` (it skips until the file exists) and
runs with `make test`:

- A line that names the judge or reviewer together with a verdict (READY, REVISE, NEEDS_HUMAN,
  ABORT) sits on a slide that says "advisory".
- "trained", "training" or "train" never appears within 80 characters of the judge or reviewer,
  except as "never trained" or "not trained".
- A slide with an "N of M", "agreement" or "false READY" carries a `badge:` key line or a
  `badge>` chat line saying what the number is.
- No Telegram chat ids, bot tokens, UUID-shaped machine ids or `api.maritime.sh/a/` URLs
  anywhere in the file, and Weave call links only on a slide with a `badge:`.
- Once the first 20 lines say `status: final`, no `{{PLACEHOLDER}}` remains (outside HTML
  comments).
- Every slide has exactly one `# ` heading; a `class: big` slide may use one `## ` instead.

Brand rules the page itself keeps: green is Approve (and the thing you press), red is Reject,
gold is value, and amber only ever means "not live".

## Deploying

```sh
cd site
vercel deploy          # a preview URL
vercel deploy --prod   # only with Carter's go-ahead: publication is his call
```

`vercel.json` turns on clean URLs (`/slides` serves `slides.html`) and sends
`X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin` and
`X-Frame-Options: SAMEORIGIN` (same-origin, not deny, because the speaker view frames the
deck). `.vercelignore` keeps this README off the site.

## Third-party code

Loaded from jsdelivr at pinned versions with Subresource Integrity: reveal.js 5.2.1 with its
Markdown and Notes plugins, and qrcode-generator 1.4.4 (the QR code is drawn client-side,
nothing is sent anywhere). Anton and JetBrains Mono come from Google Fonts. Nothing else leaves
the site. reveal.css is deliberately not loaded; `assets/slides.css` carries the structural
rules the deck uses. The deck engine is ported from the Slop Bot deck (slopbot.fyi) and
re-skinned.
