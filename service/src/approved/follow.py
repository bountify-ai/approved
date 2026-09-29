"""Read-only client for the approval.md facade's ``GET /log/follow``, with chain continuity.

The facade contract (approval-md-hosted ``image/README.md`` "Shape"): ``/log/follow`` answers
the TENANT credential only; ``/verbs``, ``/verb/<name>`` and ``/hook/<harness>`` belong to the
agent credential. Maritime's public proxy strips ``Authorization``, so the same
``Bearer <token>`` value also rides in ``X-Approval-Authorization``, which the image moves
across only when ``Authorization`` is absent. This module sends both, as ``scripts/monitor.mjs``
does.

Paging (core ``src/serve/follow.ts``): ``?from=<seq>&limit=<n>&cursor_hash=<hash>`` returns
``{records, cursor, caught_up}``. The cursor is exclusive and is the caller's to keep. The
daemon verifies the chain from genesis before answering; this client re-checks the links it
receives, the way ``validateFollowPage`` in approval-md-hosted
``scripts/gate-placement/lib.mjs`` does:

* the first record's ``prev`` equals the cursor hash (``null`` only at genesis);
* ``seq`` is contiguous from the cursor;
* each record's ``prev`` is the previous record's ``hash``;
* the returned cursor is the last record (or the unchanged cursor for an empty page).

* every record's ``hash`` recomputes: ``alg`` must be ``sha256/jcs`` and ``hash`` must equal
  SHA-256 over the RFC 8785 (JCS) serialization of the record without ``hash`` (core SPEC
  section 8; verified against real runtime output in ``tests/fixtures/facade``).

A mismatch is a :class:`ChainBreakError`. The caller stops judging; it never skips ahead.

What this does and does not prove: recomputation catches corrupted or edited records and a
continuity check pins everything after the cursor to what the judge already read. It does
not authenticate new records: whoever controls the stream can recompute hashes for records
they invent. Only TLS does that, which is why a non-loopback facade must be ``https``.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from .state import Cursor

__all__ = [
    "FOLLOW_PATH",
    "ChainBreakError",
    "CredentialError",
    "FacadeClient",
    "FollowError",
    "FollowPage",
    "LogRecord",
    "jcs",
    "record_hash",
    "verify_page",
]

FOLLOW_PATH = "/log/follow"

_HASH = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class FollowError(RuntimeError):
    """The page could not be fetched or read. Transient: retry later, cursor unchanged."""


class CredentialError(FollowError):
    """The facade refused the credential (401/403). Retried with backoff, surfaced loudly."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"facade refused the credential: HTTP {status} {code}")
        self.status = status
        self.code = code


class ChainBreakError(RuntimeError):
    """The log does not continue from the cursor. Judging stops; nothing is skipped."""

    def __init__(self, reason: str, at_seq: int) -> None:
        super().__init__(f"chain-break at seq {at_seq}: {reason}")
        self.reason = reason
        self.at_seq = at_seq


class LogRecord(BaseModel):
    """One verified log record (core ``schema/event.schema.json``). Unknown fields kept."""

    model_config = ConfigDict(extra="allow", frozen=True)

    seq: int
    hash: str
    prev: str | None
    event: str
    actor: str
    ts: str
    task: str | None = None
    action_key: str | None = None
    payload: dict[str, Any] = {}


@dataclass(frozen=True)
class FollowPage:
    records: list[LogRecord]
    cursor: Cursor
    caught_up: bool
    #: seq -> reason, for records whose links hold but whose content does not verify
    #: (``hash-mismatch``, ``record-unverifiable``, ``alg-unsupported``, ``record-malformed``).
    #: Not terminal: the caller skips them (never judges them) and keeps following.
    unverifiable: dict[int, str] = field(default_factory=dict)


HASH_ALG = "sha256/jcs"


_MAX_SAFE_INT = 2**53


def _jcs_number(value: float) -> str:
    """ECMAScript ``Number::toString`` (RFC 8785 section 3.2.2.3), exactly as core's
    ``JSON.stringify`` emits it: ``0.00005``, ``1e-7``, ``1e+21``, ``-0`` as ``0``."""
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # repr() gives the shortest round-trip digits, the same digits ECMAScript chooses.
    exponent_digits = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(str(d) for d in exponent_digits.digits)
    k = len(digits)
    n = int(exponent_digits.exponent) + k  # value = 0.digits * 10**n
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
        text = f"{mantissa}e{'+' if e > 0 else '-'}{abs(e)}"
    return sign + text


def _jcs_string(value: str) -> str:
    """ECMAScript ``QuoteJSONString`` (well-formed ``JSON.stringify``): short escapes, lowercase
    ``\\u00xx`` controls, lone surrogates escaped as ``\\udxxx``, everything else literal."""
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif code < 0x20 or 0xD800 <= code <= 0xDFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


_SHORT_ESCAPES = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def jcs(value: object) -> str:
    """RFC 8785 canonical JSON, mirroring core ``src/core/jcs.ts`` (which delegates numbers and
    strings to ``JSON.stringify``). Values JSON.parse would give JavaScript are handled; an
    integer beyond 2**53 is first rounded to a double, as JavaScript would have parsed it."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value) if abs(value) < _MAX_SAFE_INT else _jcs_number(float(value))
    if isinstance(value, float):
        return _jcs_number(value)
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, list):
        return "[" + ",".join(jcs(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: _utf16_key(str(k)))
        return "{" + ",".join(_jcs_string(str(k)) + ":" + jcs(value[k]) for k in keys) + "}"
    raise ValueError(f"unsupported JSON value {type(value).__name__}")


def record_hash(record: dict[str, object]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    # Lone surrogates were escaped by _jcs_string, so the text is always valid UTF-8.
    return hashlib.sha256(jcs(body).encode("utf-8")).hexdigest()


def _safe_code(value: object) -> str:
    """A closed-vocabulary code from a response, or a placeholder. Never free text."""
    if isinstance(value, str) and _CODE.match(value):
        return value
    return "unrecognised"


def verify_page(body: object, cursor: Cursor) -> FollowPage:
    """Validate one ``/log/follow`` body against ``cursor``.

    Raises :class:`FollowError` for a body that is not a follow page at all (a proxy error
    page, say), and :class:`ChainBreakError` for a page whose LINKS do not continue the chain
    from ``cursor`` (a seq gap or duplicate, a ``prev`` that is not the previous ``hash``, a
    malformed hash, a cursor that is not the last record): that is terminal.

    A record whose links hold but whose content does not recompute to its hash, or cannot be
    encoded, is NOT terminal. Agent-supplied text inside a record must not be able to halt the
    judge, so such a record is returned in ``unverifiable`` and the caller skips it.
    """
    if not isinstance(body, dict):
        raise FollowError("follow body is not a JSON object")
    records = body.get("records")
    next_cursor = body.get("cursor")
    caught_up = body.get("caught_up")
    if not isinstance(records, list) or not isinstance(next_cursor, dict):
        raise FollowError("follow body lacks records[] or cursor")
    if not isinstance(caught_up, bool):
        raise FollowError("follow body lacks caught_up")

    expect_seq = cursor.seq + 1
    expect_prev = cursor.hash
    parsed: list[LogRecord] = []
    unverifiable: dict[int, str] = {}
    for raw in records:
        if not isinstance(raw, dict):
            raise ChainBreakError("record-not-object", expect_seq)
        seq = raw.get("seq")
        rhash = raw.get("hash")
        prev = raw.get("prev")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq != expect_seq:
            raise ChainBreakError("seq-not-contiguous", expect_seq)
        if not isinstance(rhash, str) or not _HASH.match(rhash):
            raise ChainBreakError("hash-malformed", expect_seq)
        if prev != expect_prev:
            raise ChainBreakError("prev-mismatch", expect_seq)
        problem: str | None = None
        if raw.get("alg") != HASH_ALG:
            problem = "alg-unsupported"
        else:
            try:
                if record_hash(raw) != rhash:
                    problem = "hash-mismatch"
            except (ValueError, RecursionError):
                problem = "record-unverifiable"
        record: LogRecord | None = None
        if problem is None:
            try:
                record = LogRecord.model_validate(raw)
            except ValidationError:
                problem = "record-malformed"
        if record is None:
            unverifiable[seq] = problem or "record-unverifiable"
            record = _placeholder(raw, seq, rhash, prev)
        parsed.append(record)
        expect_seq = seq + 1
        expect_prev = rhash

    last_seq = expect_seq - 1
    if next_cursor.get("seq") != last_seq or next_cursor.get("hash") != expect_prev:
        raise ChainBreakError("cursor-mismatch", last_seq)
    return FollowPage(
        records=parsed,
        cursor=Cursor(seq=last_seq, hash=expect_prev),
        caught_up=caught_up,
        unverifiable=unverifiable,
    )


def _placeholder(raw: dict[str, Any], seq: int, rhash: str, prev: str | None) -> LogRecord:
    """A stand-in for a record that did not verify: enough to advance past it, nothing more."""
    event = raw.get("event")
    key = raw.get("action_key")
    return LogRecord.model_construct(
        seq=seq,
        hash=rhash,
        prev=prev,
        event=event if isinstance(event, str) else "unknown",
        actor="unverified",
        ts="",
        task=None,
        action_key=key if isinstance(key, str) else None,
        payload={},
    )


class FacadeClient:
    """The judge's only door to the tenant's gate: ``GET /log/follow``, tenant credential.

    There is deliberately no method for any other route. The judge never writes.
    """

    def __init__(
        self,
        base_url: str,
        tenant_token: SecretStr,
        *,
        timeout_s: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = tenant_token
        self._client = client or httpx.Client(timeout=timeout_s, follow_redirects=False)
        self._timeout = timeout_s

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        bearer = f"Bearer {self._token.get_secret_value()}"
        return {
            "Authorization": bearer,
            "X-Approval-Authorization": bearer,
            "Accept": "application/json",
        }

    def follow_page(self, cursor: Cursor, limit: int) -> FollowPage:
        params: dict[str, str] = {"from": str(cursor.seq), "limit": str(limit)}
        if cursor.hash is not None:
            params["cursor_hash"] = cursor.hash
        try:
            response = self._client.get(
                self._base + FOLLOW_PATH,
                params=params,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # The exception text can carry the URL; report the class only.
            raise FollowError(f"transport: {type(exc).__name__}") from None

        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        code = _safe_code(error.get("code")) if isinstance(error, dict) else "none"

        if response.status_code in (401, 403):
            raise CredentialError(response.status_code, code)
        if response.status_code == 409 and code == "integrity":
            # The daemon refused our cursor: the retained prefix changed (doc 02 section 6).
            reason = _safe_code(error.get("reason")) if isinstance(error, dict) else "none"
            raise ChainBreakError(f"facade-integrity:{reason}", cursor.seq)
        if response.status_code != 200:
            raise FollowError(f"HTTP {response.status_code} {code}")
        return verify_page(body, cursor)
