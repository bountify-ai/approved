"""Wording rules for the demo deck copy, site/copy/slides.md.

The deck must never let the judge look like it decides or learns: a verdict word next to the
judge sits on a slide that says "advisory", and "trained" appears near the judge only as
"never trained" or "not trained". Every number (an "N of M", an agreement rate, a false-READY
count) wears a badge saying what it is. No private identifiers reach a public page: Telegram
chat ids, bot tokens, machine ids, Maritime app URLs, and Weave call links (those only on a
badged slide). Copy marked `status: final` has no {{PLACEHOLDERS}} left, and every slide has
one H1 (a `class: big` shout may use `## ` instead).

Each rule also runs against inline fixtures in tmp_path, so the rules are exercised before the
real copy exists. Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SLIDES = ROOT / "site" / "copy" / "slides.md"

JUDGE = re.compile(r"\b(?:judge|reviewer)", re.I)
VERDICT = re.compile(r"\b(?:READY|REVISE|NEEDS_HUMAN|ABORT)\b")
ADVISORY = re.compile(r"advisory", re.I)
TRAINED = re.compile(r"\btrain(ed|ing)?\b", re.I)
EXEMPT_TRAINED = re.compile(r"\b(?:never|not)\s+trained\b", re.I)
NEAR = 80
NUMBER_CLAIM = re.compile(
    r"\d\s+of\s+\d|\d[^\n]{0,24}(?:agreement|false READY)|(?:agreement|false READY)[^\n]{0,24}\d",
    re.I,
)
BADGE_KEY = re.compile(r"^badge:[ \t]*\S", re.M)
BADGE_CHAT = re.compile(r"^[ \t]*badge>[ \t]*\S", re.M)
CLASS_KEY = re.compile(r"^class:[ \t]*(\S+)", re.M)
PLACEHOLDER = re.compile(r"\{\{\s*[A-Z0-9_]+\s*\}\}")
FINAL = "status: final"
PRIVATE: dict[str, re.Pattern[str]] = {
    "Telegram chat id": re.compile(r"-?\d{9,}"),
    "bot token": re.compile(r"\d{8,}:[A-Za-z0-9_-]{30,}"),
    "UUID-shaped machine id": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-", re.I),
    "Maritime app URL": re.compile(r"api\.maritime\.sh/a/", re.I),
}
WEAVE_CALL = re.compile(r"wandb\.ai/\S*?/r/call/", re.I)


@dataclass(frozen=True)
class Slide:
    """One slide: the lines between two `---` separators, with the first line's number."""

    start: int
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def numbered(self) -> list[tuple[int, str]]:
        return [(self.start + i, line) for i, line in enumerate(self.lines)]

    def body(self) -> list[tuple[int, str]]:
        """Numbered lines before the speaker notes, outside fenced code blocks."""
        out: list[tuple[int, str]] = []
        fence = ""
        for number, line in self.numbered():
            stripped = line.strip()
            if not fence and re.match(r"^Note:", line):
                break
            marker = stripped[:3]
            if marker in ("```", "~~~"):
                if not fence:
                    fence = marker
                elif marker == fence:
                    fence = ""
                continue
            if not fence:
                out.append((number, line))
        return out

    def is_empty(self) -> bool:
        text = re.sub(r"<!--.*?-->", "", self.text, flags=re.S)
        return not text.strip()


def split_slides(text: str) -> list[Slide]:
    slides: list[Slide] = []
    start = 1
    buf: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if line == "---":
            slides.append(Slide(start, tuple(buf)))
            start, buf = number + 1, []
        else:
            buf.append(line)
    slides.append(Slide(start, tuple(buf)))
    return slides


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def rule_verdicts_are_advisory(name: str, text: str) -> list[str]:
    """(a) A judge/reviewer line with a verdict word sits on a slide that says "advisory"."""
    problems = []
    for slide in split_slides(text):
        if ADVISORY.search(slide.text):
            continue
        for number, line in slide.numbered():
            if JUDGE.search(line) and VERDICT.search(line):
                problems.append(
                    f"{name}:{number}: judge verdict on a slide without 'advisory': {line.strip()}"
                )
    return problems


def rule_judge_never_trained(name: str, text: str) -> list[str]:
    """(b) "train(ed|ing)" within 80 characters of judge/reviewer only as never/not trained."""
    exempt = [m.span() for m in EXEMPT_TRAINED.finditer(text)]
    judges = [m.span() for m in JUDGE.finditer(text)]
    problems = []
    for match in TRAINED.finditer(text):
        start, end = match.span()
        if any(a <= start and end <= b for a, b in exempt):
            continue
        if any(max(0, js - end, start - je) <= NEAR for js, je in judges):
            line = _line_of(text, start)
            problems.append(
                f"{name}:{line}: {match.group(0)!r} near the judge "
                f"(only 'never trained' or 'not trained' may be)"
            )
    return problems


def rule_numbers_wear_a_badge(name: str, text: str) -> list[str]:
    """(c) An "N of M", or a number beside "agreement" or "false READY", needs a badge."""
    problems = []
    for slide in split_slides(text):
        match = NUMBER_CLAIM.search(slide.text)
        if not match:
            continue
        if BADGE_KEY.search(slide.text) or BADGE_CHAT.search(slide.text):
            continue
        line = slide.start + _line_of(slide.text, match.start()) - 1
        problems.append(
            f"{name}:{line}: {match.group(0)!r} on a slide with no 'badge:' line "
            f"or 'badge>' chat line"
        )
    return problems


def rule_no_private_identifiers(name: str, text: str) -> list[str]:
    """(d) No chat ids, bot tokens, machine ids, Maritime app URLs; Weave call links badged."""
    problems = []
    for label, pattern in PRIVATE.items():
        for match in pattern.finditer(text):
            line = _line_of(text, match.start())
            problems.append(f"{name}:{line}: {label}: {match.group(0)!r}")
    for slide in split_slides(text):
        if BADGE_KEY.search(slide.text):
            continue
        for match in WEAVE_CALL.finditer(slide.text):
            line = slide.start + _line_of(slide.text, match.start()) - 1
            problems.append(f"{name}:{line}: Weave call link on a slide with no 'badge:' line")
    return problems


def rule_final_has_no_placeholders(name: str, text: str) -> list[str]:
    """(e) Copy marked `status: final` in its first 20 lines has no {{PLACEHOLDERS}} left."""
    if FINAL not in "\n".join(text.splitlines()[:20]):
        return []
    # An HTML comment never renders (the copy's header comment documents the {{...}} grammar);
    # blank it out line for line so the numbers still match the file.
    shown = re.sub(r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    return [
        f"{name}:{_line_of(shown, m.start())}: placeholder in final copy: {m.group(0)}"
        for m in PLACEHOLDER.finditer(shown)
    ]


def rule_one_h1_per_slide(name: str, text: str) -> list[str]:
    """(f) Exactly one `# ` heading per slide; a `class: big` slide may use one `## ` instead."""
    problems = []
    for slide in split_slides(text):
        if slide.is_empty():
            continue
        body = slide.body()
        h1 = [number for number, line in body if line.startswith("# ")]
        h2 = [number for number, line in body if line.startswith("## ")]
        classes = [m.group(1).lower() for m in CLASS_KEY.finditer(slide.text)]
        if len(h1) == 1 or ("big" in classes and not h1 and len(h2) == 1):
            continue
        where = h1[1] if len(h1) > 1 else slide.start
        wanted = "one '# ' (or one '## ' on a class: big slide)" if "big" in classes else "one '# '"
        problems.append(f"{name}:{where}: slide has {len(h1)} H1 headings, wants {wanted}")
    return problems


Rule = Callable[[str, str], list[str]]
RULES: dict[str, Rule] = {
    "verdicts_are_advisory": rule_verdicts_are_advisory,
    "judge_never_trained": rule_judge_never_trained,
    "numbers_wear_a_badge": rule_numbers_wear_a_badge,
    "no_private_identifiers": rule_no_private_identifiers,
    "final_has_no_placeholders": rule_final_has_no_placeholders,
    "one_h1_per_slide": rule_one_h1_per_slide,
}


def check(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [problem for rule in RULES.values() for problem in rule(path.name, text)]


# ---- the real copy ------------------------------------------------------------------------


@pytest.mark.parametrize("rule", list(RULES))
def test_deck_copy_keeps_the_wording_rules(rule: str) -> None:
    if not SLIDES.is_file():
        pytest.skip("site/copy/slides.md does not exist yet (the deck copy is written separately)")
    problems = RULES[rule](SLIDES.name, SLIDES.read_text(encoding="utf-8"))
    assert not problems, "\n".join(problems)


# ---- the rules against inline fixtures -----------------------------------------------------

GOOD = """<!-- status: final -->
class: title

# Approved

Hosted approval.md with an advisory AI judge.

---

class: big

## Nothing the agent holds can grant.

The human's tap is the only thing that grants.

---

class: chat
badge: simulated

# The judge weighs in

```chat
badge> simulated
gate> git push origin main
buttons> Approve | Reject
judge> Judge (advisory AI, not an approval): NEEDS_HUMAN
human> (taps Reject)
```

---

class: split
badge: illustrative

# Measured against people

**3 of 4** — agreement with the human, false READY count 0.

The reviewer model is never trained on tenant data.

---

class: code

# The hook

```sh
# a shell comment is not a heading
approval serve --tenant demo
```

Note:
The judge is not trained on anything here; it reads https://wandb.ai/team/proj/weave.
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "slides.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_good_fixture_passes_every_rule(tmp_path: Path) -> None:
    assert check(_write(tmp_path, GOOD)) == []


def test_slides_split_on_exact_separator_lines() -> None:
    slides = split_slides("# One\n---\n# Two\n----\ntext\n --- \n")
    assert [s.start for s in slides] == [1, 3]
    assert slides[1].lines == ("# Two", "----", "text", " --- ")


_TOKEN = "1" * 9 + ":" + "a" * 35
_UUID = "0a1b2c3d" + "-4e5f-6789-abcd-ef0123456789"

BAD: dict[str, tuple[Rule, str, str]] = {
    "verdict without advisory": (
        rule_verdicts_are_advisory,
        "# Judge\n\nThe judge says READY.\n",
        "slides.md:3: judge verdict",
    ),
    "verdict in chat without advisory": (
        rule_verdicts_are_advisory,
        "# Chat\n\n```chat\njudge> ABORT, this deletes prod\n```\n",
        "slides.md:4: judge verdict",
    ),
    "trained near the judge": (
        rule_judge_never_trained,
        "# Learns\n\nThe judge is trained on every decision.\n",
        "slides.md:3: 'trained' near the judge",
    ),
    "training near the reviewer": (
        rule_judge_never_trained,
        "# Learns\n\nFeedback goes into training the Reviewer.\n",
        "slides.md:3: 'training' near the judge",
    ),
    "never training is not exempt": (
        rule_judge_never_trained,
        "# Learns\n\nThe judge is never training.\n",
        "slides.md:3: 'training' near the judge",
    ),
    "N of M without a badge": (
        rule_numbers_wear_a_badge,
        "# Score\n\nThe judge matched 3 of 4 decisions.\n",
        "slides.md:3: '3 of 4'",
    ),
    "agreement without a badge": (
        rule_numbers_wear_a_badge,
        "# Score\n\nsome prose\nAgreement 1.0 on the set.\n",
        "slides.md:4: 'Agreement 1.0'",
    ),
    "false READY without a badge": (
        rule_numbers_wear_a_badge,
        "# Score\n\n0 of 7 false READY.\n",
        "slides.md:3: '0 of 7'",
    ),
    "chat id": (
        rule_no_private_identifiers,
        "# Ids\n\nchat -1001234567890\n",
        "slides.md:3: Telegram chat id",
    ),
    "bot token": (
        rule_no_private_identifiers,
        f"# Ids\n\n{_TOKEN}\n",
        "slides.md:3: bot token",
    ),
    "machine id": (
        rule_no_private_identifiers,
        f"# Ids\n\nmachine {_UUID}\n",
        "slides.md:3: UUID-shaped machine id",
    ),
    "maritime app url": (
        rule_no_private_identifiers,
        "# Ids\n\nhttps://api.maritime.sh/a/judge\n",
        "slides.md:3: Maritime app URL",
    ),
    "weave call link without a badge": (
        rule_no_private_identifiers,
        "# Trace\n\nhttps://wandb.ai/team/proj/r/call/0199\n",
        "slides.md:3: Weave call link",
    ),
    "placeholder in final copy": (
        rule_final_has_no_placeholders,
        "<!-- status: final -->\n# Join\n\n{{ QR_LINK }}\n",
        "slides.md:4: placeholder in final copy",
    ),
    "placeholder after a comment that names one": (
        rule_final_has_no_placeholders,
        "<!--\n  status: final\n  qr: {{PLACEHOLDER}}\n-->\n# Join\n\n{{CONTACT_LINK}}\n",
        "slides.md:7: placeholder in final copy: {{CONTACT_LINK}}",
    ),
    "no H1": (
        rule_one_h1_per_slide,
        "# One\n\n---\n\n## Two\n",
        "slides.md:4: slide has 0 H1 headings",
    ),
    "two H1s": (
        rule_one_h1_per_slide,
        "# One\n\n# Again\n",
        "slides.md:3: slide has 2 H1 headings",
    ),
    "big slide with two H2s": (
        rule_one_h1_per_slide,
        "class: big\n\n## One\n\n## Two\n",
        "slides.md:1: slide has 0 H1 headings",
    ),
}


@pytest.mark.parametrize("case", list(BAD))
def test_bad_fixture_is_named_by_its_rule(tmp_path: Path, case: str) -> None:
    rule, text, expected = BAD[case]
    path = _write(tmp_path, text)
    problems = rule(path.name, path.read_text(encoding="utf-8"))
    assert any(p.startswith(expected) for p in problems), problems


def test_badges_and_exemptions_clear_the_rules(tmp_path: Path) -> None:
    text = (
        "badge: live\n\n# Trace\n\n3 of 4, agreement, false READY: "
        "https://wandb.ai/team/proj/r/call/0199\n\n---\n\n"
        "# Draft\n\n{{QR_LINK}} is fine until the copy is marked final.\n"
    )
    assert check(_write(tmp_path, text)) == []


def test_placeholders_inside_comments_do_not_count() -> None:
    text = "<!--\n  status: final\n  qr: {{PLACEHOLDER}}\n-->\n# Join\n\nDone.\n"
    assert rule_final_has_no_placeholders("slides.md", text) == []
