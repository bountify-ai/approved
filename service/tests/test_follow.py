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
        (lambda b: b["records"][0].__setitem__("event", 7), "hash-mismatch"),
        (lambda b: b["records"][2]["payload"].__setitem__("summary", "ls"), "hash-mismatch"),
        (lambda b: b["records"][0].__setitem__("alg", "sha1/none"), "alg-unsupported"),
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


def test_a_well_hashed_but_malformed_record_is_still_refused() -> None:
    from approved.follow import record_hash

    body = copy.deepcopy(GENESIS)
    bad = body["records"][0]
    bad["event"] = 7
    bad["hash"] = record_hash(bad)
    body["records"][1]["prev"] = bad["hash"]  # keep the links; the next record will not verify
    with pytest.raises(ChainBreakError) as info:
        verify_page({**body, "records": [bad]}, Cursor())
    assert info.value.reason == "record-malformed"


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
