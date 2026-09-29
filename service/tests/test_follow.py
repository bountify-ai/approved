"""Follow paging and chain continuity (invariant 5), against real captured facade answers."""

from __future__ import annotations

import contextlib
import copy

import httpx
import pytest
from pydantic import SecretStr

from approved.follow import (
    ChainBreakError,
    CredentialError,
    FacadeClient,
    FollowError,
    verify_page,
)
from approved.state import Cursor

from .fakes import (
    AGENT_TOKEN,
    FACADE_URL,
    TENANT_TOKEN,
    FakeFacade,
    ReplayFacade,
    load_fixture,
    request_event,
)

GENESIS = load_fixture("follow-genesis.json")["body"]
LATEST = load_fixture("follow-genesis-latest.json")["body"]


def test_real_genesis_page_verifies_from_empty_cursor() -> None:
    page = verify_page(GENESIS, Cursor())
    assert [r.seq for r in page.records] == [1, 2, 3]
    assert page.records[2].event == "approval.requested"
    assert page.records[2].payload["class"] == "vcs.push.main"
    assert page.cursor == Cursor(seq=3, hash=GENESIS["cursor"]["hash"])
    assert page.caught_up is True


def test_real_page_continues_from_a_mid_log_cursor() -> None:
    after3 = load_fixture("follow-genesis-latest.json")["body"]
    body = {**after3, "records": after3["records"][3:]}
    page = verify_page(body, Cursor(seq=3, hash=GENESIS["cursor"]["hash"]))
    assert page.records[0].seq == 4
    assert page.cursor.seq == 19


def test_unknown_record_fields_are_preserved() -> None:
    page = verify_page(LATEST, Cursor())
    expired = next(r for r in page.records if r.event == "approval.expired")
    assert expired.model_extra is not None
    assert expired.model_extra["daemon"] == "daemon-fixture-1"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda b: b["records"][1].__setitem__("prev", "0" * 64), "prev-mismatch"),
        (lambda b: b["records"][1].__setitem__("seq", 5), "seq-not-contiguous"),
        (lambda b: b["records"].pop(1), "seq-not-contiguous"),
        (lambda b: b["records"][0].__setitem__("hash", "not-a-hash"), "hash-malformed"),
        (lambda b: b["cursor"].__setitem__("seq", 2), "cursor-mismatch"),
        (lambda b: b["cursor"].__setitem__("hash", "f" * 64), "cursor-mismatch"),
    ],
)
def test_broken_links_are_chain_breaks(mutate, reason: str) -> None:
    # Attack input: a copy of the real page, altered in memory. The file is never changed.
    body = copy.deepcopy(GENESIS)
    mutate(body)
    with pytest.raises(ChainBreakError) as info:
        verify_page(body, Cursor())
    assert info.value.reason == reason


def test_hashes_are_recomputed_with_the_runtime_scheme() -> None:
    """Every captured record's hash is SHA-256 over JCS without ``hash`` (core SPEC 8)."""
    from approved.follow import record_hash

    for record in LATEST["records"]:
        assert record_hash(record) == record["hash"]


@pytest.mark.parametrize(
    ("mutate", "seq", "reason"),
    [
        (lambda b: b["records"][0].__setitem__("event", 7), 1, "hash-mismatch"),
        (lambda b: b["records"][2]["payload"].__setitem__("summary", "ls"), 3, "hash-mismatch"),
        (lambda b: b["records"][0].__setitem__("alg", "sha1/none"), 1, "alg-unsupported"),
        (
            lambda b: b["records"][1]["payload"].__setitem__("n", float("inf")),
            2,
            "record-unverifiable",
        ),
    ],
)
def test_content_failures_are_not_terminal(mutate, seq: int, reason: str) -> None:
    """Links hold; content does not verify. Agent text must not halt the judge: the record is
    marked unverifiable and the page still verifies."""
    body = copy.deepcopy(GENESIS)
    mutate(body)
    page = verify_page(body, Cursor())
    assert page.unverifiable == {seq: reason}
    assert [r.seq for r in page.records] == [1, 2, 3]
    assert page.cursor.seq == 3


def test_a_well_hashed_but_malformed_record_is_unverifiable_not_terminal() -> None:
    from approved.follow import record_hash

    body = copy.deepcopy(GENESIS)
    bad = body["records"][0]
    bad["actor"] = 7  # well hashed below, but not a valid record
    bad["hash"] = record_hash(bad)
    page = verify_page(
        {**body, "records": [bad], "cursor": {"seq": 1, "hash": bad["hash"]}}, Cursor()
    )
    assert page.unverifiable == {1: "record-malformed"}


# Output of core's own canonicalizer (approval-md dist/src/core/jcs.js, `canonicalize`) for the
# value below, captured with `node -e` on 2026-09-29. It covers ECMAScript number formatting
# (0.00005, 1e-7, -0, 1e+21, an integer past 2**53), a lone surrogate, U+2028, and UTF-16 key
# order (a surrogate pair sorts before U+FFFF).
CORE_JCS = (
    '{"a":0.00005,"b":1e-7,"c":0,"d":12345678901234567000,"e":"\\ud800",'
    '"f":{"\u00e9":"\U0001f600\u2028","\U0001f600":1,"\uffff":2},'
    '"g":1e+21,"h":1.23456e-8,"i":1.5e+300}'
)


def test_jcs_matches_core_byte_for_byte() -> None:
    from approved.follow import jcs

    value = {
        "a": 0.00005,
        "b": 1e-7,
        "c": -0.0,
        "d": 12345678901234567890,
        "e": "\ud800",
        "f": {"\u00e9": "\U0001f600\u2028", "\U0001f600": 1, "\uffff": 2},
        "g": 1e21,
        "h": 123.456e-10,
        "i": 1.5e300,
    }
    assert jcs(value) == CORE_JCS


def test_a_record_with_a_lone_surrogate_verifies() -> None:
    from approved.follow import record_hash

    body = copy.deepcopy(GENESIS)
    rec = body["records"][0]
    rec["payload"]["note"] = "bad \ud800 text 0.00005"
    rec["hash"] = record_hash(rec)
    page = verify_page(
        {**body, "records": [rec], "cursor": {"seq": 1, "hash": rec["hash"]}}, Cursor()
    )
    assert page.unverifiable == {}


def test_first_record_must_link_to_cursor_hash() -> None:
    body = {**LATEST, "records": LATEST["records"][3:]}
    with pytest.raises(ChainBreakError) as info:
        verify_page(body, Cursor(seq=3, hash="a" * 64))
    assert info.value.reason == "prev-mismatch"
    assert info.value.at_seq == 4


def test_empty_page_must_keep_the_cursor() -> None:
    head = Cursor(seq=3, hash=GENESIS["cursor"]["hash"])
    same = {"records": [], "cursor": {"seq": 3, "hash": head.hash}, "caught_up": True}
    assert verify_page(same, head).cursor == head
    moved = {"records": [], "cursor": {"seq": 4, "hash": head.hash}, "caught_up": True}
    with pytest.raises(ChainBreakError):
        verify_page(moved, head)


@pytest.mark.parametrize(
    "body",
    [None, [], {"records": []}, {"records": [], "cursor": {}}, {"records": {}, "cursor": {}}],
)
def test_non_follow_bodies_are_transient_errors(body) -> None:
    with pytest.raises(FollowError):
        verify_page(body, Cursor())


def _client(fake: FakeFacade, token: str = TENANT_TOKEN) -> FacadeClient:
    return FacadeClient(FACADE_URL, SecretStr(token), client=fake.client())


def test_client_sends_tenant_credential_on_both_carriers_and_cursor_params() -> None:
    fake = FakeFacade()
    fake.append(request_event("k1"), request_event("k2"))
    client = _client(fake)
    first = client.follow_page(Cursor(), 1)
    client.follow_page(first.cursor, 1)
    req = fake.requests[-1]
    assert req.method == "GET"
    assert req.url.path.endswith("/log/follow")
    assert req.headers["authorization"] == f"Bearer {TENANT_TOKEN}"
    assert req.headers["x-approval-authorization"] == f"Bearer {TENANT_TOKEN}"
    assert req.url.params["from"] == "1"
    assert req.url.params["cursor_hash"] == first.cursor.hash
    assert req.url.params["limit"] == "1"
    assert "cursor_hash" not in fake.requests[0].url.params
    assert fake.violations == []


def test_facade_integrity_refusal_is_a_chain_break() -> None:
    fake = ReplayFacade(body=load_fixture("follow-cursor-mismatch.json")["body"], status=409)
    with pytest.raises(ChainBreakError) as info:
        _client(fake).follow_page(Cursor(seq=3, hash="0" * 64), 10)
    assert info.value.reason == "facade-integrity:cursor-mismatch"


def test_agent_forbidden_is_a_credential_error() -> None:
    fake = ReplayFacade(body=load_fixture("follow-agent-forbidden.json")["body"], status=403)
    with pytest.raises(CredentialError) as info:
        _client(fake).follow_page(Cursor(), 10)
    assert info.value.code == "serve-agent-forbidden"


def test_transport_error_hides_the_url() -> None:
    fake = FakeFacade(script=[httpx.ConnectError("boom https://facade.test/secret-path")])
    with pytest.raises(FollowError) as info:
        _client(fake).follow_page(Cursor(), 10)
    assert "facade.test" not in str(info.value)
    assert str(info.value) == "transport: ConnectError"


def test_server_error_is_transient() -> None:
    fake = FakeFacade(script=[httpx.Response(502, text="<html>bad gateway</html>")])
    with pytest.raises(FollowError, match="HTTP 502"):
        _client(fake).follow_page(Cursor(), 10)


def test_fake_facade_guard_catches_writes_and_agent_credential() -> None:
    """The guard every worker test relies on really does catch both violations."""
    fake = FakeFacade()
    http = fake.client()
    http.post(f"{FACADE_URL}/verb/request", headers={"Authorization": f"Bearer {TENANT_TOKEN}"})
    http.post(f"{FACADE_URL}/hook/hermes", headers={"Authorization": f"Bearer {TENANT_TOKEN}"})
    http.post(f"{FACADE_URL}/verb/queue", headers={"Authorization": f"Bearer {TENANT_TOKEN}"})
    with contextlib.suppress(CredentialError):
        _client(fake, token=AGENT_TOKEN).follow_page(Cursor(), 1)
    assert any("forbidden route ('POST'" in v for v in fake.violations)
    assert sum("forbidden route" in v for v in fake.violations) == 3
    assert any("agent credential" in v for v in fake.violations)
