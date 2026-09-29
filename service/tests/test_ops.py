"""Operator CLI against a fake ``maritime`` that records argv and stdin."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from approved.__main__ import main
from approved.ops.maritime import Maritime, MaritimeError, SecretGuard
from approved.ops.names import ProtectedName, is_protected
from approved.ops.provision import ProvisionError, ProvisionOptions, attest_command, provision

FAKE = Path(__file__).with_name("fake_maritime.py")
POLICY = b'# Policy\n\n```yaml approval-policy\nversion: "0.1"\n```\n'
BOT_TOKEN = "7001:real-looking-bot-token-value-abcdef"
JUDGE_BOT = "7002:the-judges-own-bot-token-value-12345"


@dataclass
class FakeMaritime:
    state_path: Path
    log_path: Path

    def calls(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(x) for x in self.log_path.read_text().splitlines() if x]

    def argvs(self, verb: str | None = None) -> list[list[str]]:
        """Each call's argv without the leading global options (``--json``)."""
        out = []
        for call in self.calls():
            argv = list(call["argv"])
            while argv and argv[0] in ("--json", "--verbose"):
                argv.pop(0)
            if verb is None or (argv and argv[0] == verb):
                out.append(argv)
        return out

    @property
    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())

    def set(self, **values: Any) -> None:
        state = self.state
        state.update(values)
        self.state_path.write_text(json.dumps(state))


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeMaritime:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "maritime"
    binary.write_text(f"#!{sys.executable}\n" + FAKE.read_text())
    binary.chmod(0o755)
    fm = FakeMaritime(tmp_path / "maritime-state.json", tmp_path / "maritime-log.jsonl")
    fm.state_path.write_text(json.dumps({"agents": []}))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_MARITIME_STATE", str(fm.state_path))
    monkeypatch.setenv("FAKE_MARITIME_LOG", str(fm.log_path))
    monkeypatch.chdir(tmp_path)
    return fm


def _http() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if request.url.path.endswith("/health") else 401)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _opts(tmp_path: Path, lines: list[str], **kw: Any) -> ProvisionOptions:
    policy = tmp_path / "APPROVAL.md"
    policy.write_bytes(POLICY)
    token_file = tmp_path / "bot-token"
    token_file.write_text(BOT_TOKEN + "\n")
    token_file.chmod(0o600)
    judge_file = tmp_path / "judge-bot-token"
    judge_file.write_text(JUDGE_BOT + "\n")
    defaults: dict[str, Any] = {
        "tenant": "acme",
        "policy": policy,
        "approver": "carter",
        "tg_chat": "4242",
        "tg_bot_token_file": token_file,
        "judge_bot_token_file": judge_file,
        "credentials_root": tmp_path / ".approved",
        "wait_s": 5,
        "poll_s": 0,
        "out": lines.append,
        "http": _http(),
        "sleep": lambda _s: None,
    }
    defaults.update(kw)
    return ProvisionOptions(**defaults)


def _secret_values(creds: Path) -> list[str]:
    return [p.read_text().strip() for p in creds.iterdir() if p.is_file() and p.suffix != ".env"]


# ------------------------------------------------------------------ provision


def test_provision_fresh_daemon(fake: FakeMaritime, tmp_path: Path) -> None:
    lines: list[str] = []
    result = provision(Maritime(SecretGuard()), _opts(tmp_path, lines))
    assert result["facade_url"] == "https://api.maritime.sh/a/id-acme-daemon"

    creates = fake.argvs("create")
    assert len(creates) == 1
    create = creates[0]
    assert all(c["argv"][0] == "--json" for c in fake.calls())  # global, before the command
    for flag in ("--repo", "--branch", "--public", "--port"):
        assert flag in create
    assert create[create.index("--port") + 1] == "18789"
    assert "--template" not in create
    pairs = [create[i + 1] for i, a in enumerate(create) if a == "-e"]
    assert "APPROVAL_TENANT=acme" in pairs
    assert "HOSTED_ACME_TG_CHAT=4242" in pairs
    assert "APPROVAL_SERVE_HOOK_HARNESS_CAP=300s" in pairs
    names = {p.split("=", 1)[0] for p in pairs}
    assert not names & {
        "APPROVAL_SERVE_AGENT_TOKEN",
        "APPROVAL_SERVE_TENANT_TOKEN",
        "APPROVAL_TG_WEBHOOK_SECRET",
        "HOSTED_ACME_TG_BOT_TOKEN",
    }
    assert fake.calls()[1]["stdin"] == "\n"  # the create call: an empty line for any prompt

    imported = fake.state["env"]["acme-daemon"]
    assert set(imported) == {
        "APPROVAL_SERVE_AGENT_TOKEN",
        "APPROVAL_SERVE_TENANT_TOKEN",
        "APPROVAL_TG_WEBHOOK_SECRET",
        "HOSTED_ACME_TG_BOT_TOKEN",
        "APPROVAL_PUBLIC_URL",
    }
    verbs = [a[0] for a in fake.argvs()]
    assert verbs.index("env") < verbs.index("stop") < verbs.index("start")
    assert fake.state["remote_policy_sha"] == result["policy_sha256"]
    assert attest_command("acme-daemon", "acme", "carter") in "\n".join(lines)


def test_no_token_value_on_any_argv_stdin_or_output(fake: FakeMaritime, tmp_path: Path) -> None:
    lines: list[str] = []
    provision(Maritime(SecretGuard()), _opts(tmp_path, lines, hermes=True, judge=True))
    secrets = _secret_values(tmp_path / ".approved" / "acme")
    assert BOT_TOKEN in secrets
    assert JUDGE_BOT in secrets
    assert len(secrets) == 6
    everything = json.dumps(fake.calls()) + "\n".join(lines)
    for value in secrets:
        assert value not in everything
        assert value.split(":")[-1] not in everything


def test_credential_files_are_0600_in_a_0700_dir(fake: FakeMaritime, tmp_path: Path) -> None:
    provision(Maritime(SecretGuard()), _opts(tmp_path, [], hermes=True, judge=True))
    creds = tmp_path / ".approved" / "acme"
    assert stat.S_IMODE(creds.stat().st_mode) == 0o700
    files = list(creds.iterdir())
    assert any(p.suffix == ".env" for p in files)
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


def test_hermes_and_judge_get_only_their_own_credentials(
    fake: FakeMaritime, tmp_path: Path
) -> None:
    provision(
        Maritime(SecretGuard()),
        _opts(tmp_path, [], hermes=True, hermes_model="gpt-5.4", judge=True),
    )
    env = fake.state["env"]
    assert env["acme-hermes"] == ["HOSTED_ACME_FACADE_AGENT_TOKEN"]
    assert set(env["acme-judge"]) == {
        "TENANT_TOKEN",
        "CONSOLE_TOKEN",
        "JUDGE_TG_BOT_TOKEN",
        "PUBLIC_BASE_PATH",
    }
    judge_file = (tmp_path / ".approved" / "acme" / "acme-judge.env").read_text()
    assert "PUBLIC_BASE_PATH=/a/id-acme-judge" in judge_file
    hermes_create = next(a for a in fake.argvs("create") if a[1] == "acme-hermes")
    assert "--public" not in hermes_create
    assert "APPROVAL_HERMES_MODEL=gpt-5.4" in hermes_create
    assert "HOSTED_ACME_FACADE_URL=https://api.maritime.sh/a/id-acme-daemon" in hermes_create


def test_provision_is_idempotent(fake: FakeMaritime, tmp_path: Path) -> None:
    provision(Maritime(SecretGuard()), _opts(tmp_path, []))
    first = len(fake.calls())
    lines: list[str] = []
    provision(Maritime(SecretGuard()), _opts(tmp_path, lines))
    again = fake.calls()[first:]
    verbs = [c["argv"][1] for c in again]
    assert "create" not in verbs
    assert "env" not in verbs
    assert "stop" not in verbs
    assert not any("base64 -d" in json.dumps(c["argv"]) for c in again)
    assert any("reusing" in line for line in lines)
    assert any("already in place" in line for line in lines)


def test_existing_machine_without_credentials_is_refused(
    fake: FakeMaritime, tmp_path: Path
) -> None:
    fake.set(agents=[{"id": "id-acme-daemon", "name": "acme-daemon"}])
    with pytest.raises(ProvisionError, match="holds no credentials"):
        provision(Maritime(SecretGuard()), _opts(tmp_path, []))
    assert fake.argvs("create") == []


def test_without_a_bot_token_there_is_no_webhook_mode(fake: FakeMaritime, tmp_path: Path) -> None:
    provision(Maritime(SecretGuard()), _opts(tmp_path, [], tg_bot_token_file=None))
    imported = set(fake.state["env"]["acme-daemon"])
    assert "APPROVAL_TG_WEBHOOK_SECRET" not in imported
    assert "HOSTED_ACME_TG_BOT_TOKEN" not in imported
    assert "APPROVAL_SERVE_AGENT_TOKEN" in imported


def test_attest_is_printed_and_never_executed(fake: FakeMaritime, tmp_path: Path) -> None:
    lines: list[str] = []
    provision(Maritime(SecretGuard()), _opts(tmp_path, lines))
    assert not any("policy attest" in json.dumps(a) for a in fake.argvs())
    expected = (
        "maritime exec --json acme-daemon -- sh -c 'cd /data/acme && node "
        "/opt/runtime/node_modules/approval-md/cli.js policy attest --as human:carter --json'"
    )
    assert any(expected in line for line in lines)


def test_facade_not_ready_fails_closed(fake: FakeMaritime, tmp_path: Path) -> None:
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(ProvisionError, match="not ready"):
        provision(Maritime(SecretGuard()), _opts(tmp_path, [], http=down, wait_s=0))
    assert not any("base64" in json.dumps(a) for a in fake.argvs("exec"))


@pytest.mark.parametrize("tenant", ["dogfood", "my-dogfood", "dogfood-2", "x-dogfood-y"])
def test_dogfood_tenants_are_refused(fake: FakeMaritime, tmp_path: Path, tenant: str) -> None:
    with pytest.raises(ProtectedName):
        provision(Maritime(SecretGuard()), _opts(tmp_path, [], tenant=tenant))
    assert fake.calls() == []


@pytest.mark.parametrize(
    ("name", "protected"),
    [
        ("approval-dogfood", True),
        ("approval-hermes", True),
        ("approval-hermes-gated", True),
        ("approval-x16-daemon-3", True),
        ("Dogfood-thing", True),
        ("acme-hermes", False),
        ("approval-md", False),
    ],
)
def test_protected_names(name: str, protected: bool) -> None:
    assert is_protected(name) is protected


def test_cli_refuses_protected_machines(fake: FakeMaritime, capsys) -> None:
    assert main(["ask", "approval-hermes-gated", "hello"]) == 2
    assert main(["ask", "approval-x16-hermes", "hello"]) == 2
    assert main(["status", "dogfood"]) == 2
    assert main(["connect", "dogfood", "--facade-url", "https://a.test"]) == 2
    assert fake.calls() == []
    assert "refused" in capsys.readouterr().out


def test_secret_guard_blocks_before_spawning(fake: FakeMaritime) -> None:
    guard = SecretGuard(["super-secret-value-123"])
    with pytest.raises(MaritimeError, match="credential value"):
        Maritime(guard).run(["env", "set", "acme", "X=super-secret-value-123"])
    assert fake.calls() == []


# ------------------------------------------------------------------ ask


def _ask(
    fake: FakeMaritime, outcome: str, capsys, prompt: str = "list the repo"
) -> tuple[int, str]:
    fake.set(agents=[{"id": "id-acme-hermes", "name": "acme-hermes"}], ask=outcome)
    code = main(["ask", "acme-hermes", prompt, "--wait-s", "0"])
    return code, capsys.readouterr().out


def test_ask_allowed(fake: FakeMaritime, capsys) -> None:
    code, out = _ask(fake, "allowed", capsys)
    assert code == 0
    assert "the README lists three services" in out
    launch = fake.argvs("exec")[0]
    script = launch[launch.index("--") + 3]
    assert "http://127.0.0.1:8642/v1/chat/completions" in script
    assert "/command/s6-setuidgid hermes node" in script
    assert "API_SERVER_KEY" in script
    assert "setsid" in script
    assert any("rm -rf /tmp/approved-ask-" in json.dumps(a) for a in fake.argvs("exec"))


def test_ask_blocked_says_waiting_for_approval(fake: FakeMaritime, capsys) -> None:
    code, out = _ask(fake, "blocked", capsys, prompt="push main")
    assert code == 3
    assert "waiting for approval on Telegram" in out


def test_ask_timeout_leaves_the_job_and_says_how_to_collect(fake: FakeMaritime, capsys) -> None:
    code, out = _ask(fake, "timeout", capsys)
    assert code == 4
    job = re.search(r"--job ([0-9a-f]{16})", out)
    assert job is not None
    assert not any("rm -rf" in json.dumps(a) for a in fake.argvs("exec"))
    fake.set(ask="allowed")
    assert main(["ask", "acme-hermes", "--job", job.group(1), "--wait-s", "0"]) == 0


def test_ask_gateway_failure(fake: FakeMaritime, capsys) -> None:
    code, out = _ask(fake, "failed", capsys)
    assert code == 5
    assert "failed" in out


def test_ask_prompt_cannot_break_out_of_the_script(fake: FakeMaritime, capsys) -> None:
    hostile = "x'\nGATEWAY_JS\nAPPROVED_ASK_CALL\nrm -rf / #"
    code, _ = _ask(fake, "allowed", capsys, prompt=hostile)
    assert code == 0
    launch = fake.argvs("exec")[0]
    script = launch[launch.index("--") + 3]
    lines = script.splitlines()
    assert lines.count("GATEWAY_JS") == 1
    assert lines.count("APPROVED_ASK_CALL") == 1
    assert "rm -rf / #" not in lines


def test_ask_rejects_a_bad_job_id(fake: FakeMaritime) -> None:
    fake.set(agents=[{"id": "i", "name": "acme-hermes"}])
    assert main(["ask", "acme-hermes", "--job", "../../etc"]) == 2
    assert fake.argvs("exec") == []


# ------------------------------------------------------------------ status, connect


def test_status_prints_counts_and_head_only(
    fake: FakeMaritime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from approved.ops.status import status

    fake.set(
        agents=[{"id": "id-acme-daemon", "name": "acme-daemon"}],
        verify={
            "status": "clean",
            "records": 12,
            "head": {"seq": 12, "hash": "ab" * 32},
            "path": "/data/acme/.approval/log/events.jsonl",
        },
    )
    lines: list[str] = []
    report = status(Maritime(SecretGuard()), "acme", http=_http(), out=lines.append)
    text = "\n".join(lines)
    assert report["verify"] == {
        "status": "clean",
        "records": 12,
        "head_seq": 12,
        "head_hash": "abababababab",
    }
    assert "12 records, head seq 12" in text
    assert "events.jsonl" not in text
    assert "ab" * 32 not in text
    assert "public /health: 200" in text


def test_connect_writes_the_console_bundle(fake: FakeMaritime, tmp_path: Path, capsys) -> None:
    from approved.console.bundle import build_bundle

    assert main(["connect", "acme", "--facade-url", "https://api.maritime.sh/a/x"]) == 0
    out = tmp_path / "approved-connect-acme"
    bundle = build_bundle(mode="byo", tenant="acme", facade_url="https://api.maritime.sh/a/x")
    assert (out / "connect.sh").read_text() == bundle.script  # same text as the console
    assert (out / "hermes-config.yaml").read_text().strip() == (bundle.hooks_yaml or "").strip()
    assert stat.S_IMODE((out / "connect.sh").stat().st_mode) == 0o700
    assert stat.S_IMODE((out / "hermes-hook-shim.sh").stat().st_mode) == 0o755
    assert re.search(r"\b[0-9a-f]{40,}\b", (out / "connect.sh").read_text()) is None
    assert main(["connect", "acme", "--facade-url", "https://api.maritime.sh/a/x"]) == 2
    assert fake.calls() == []


@pytest.mark.parametrize(
    "args",
    [
        ["create", "acme-daemon", "--repo", "https://x.test", "--framework", "custom"],
        ["exec", "--verbose-output", "acme-daemon", "--", "sh", "-c", "true"],
        ["env", "import", "acme-daemon", "f.env", "--json"],
        ["stop", "acme-daemon", "--force"],
    ],
)
def test_fake_rejects_flags_the_real_cli_lacks(fake: FakeMaritime, args: list[str]) -> None:
    """The fake mirrors `maritime <cmd> --help` (1.7.0), so every provision/ask/status test
    above proves the CLI passes only flags that exist."""
    with pytest.raises(MaritimeError) as info:
        Maritime(SecretGuard()).run(args)
    assert info.value.exit_code == 4


def test_fake_accepts_json_after_exec_like_the_real_cli(fake: FakeMaritime) -> None:
    fake.set(agents=[{"id": "i", "name": "acme-daemon"}])
    result = Maritime(SecretGuard()).run(
        ["exec", "--json", "acme-daemon", "--", "sh", "-c", "true"]
    )
    assert result["exit_code"] == 0


# ------------------------------------------------------------------ B1: the judge's own bot


def test_judge_env_file_never_holds_the_gate_bot_value(fake: FakeMaritime, tmp_path: Path) -> None:
    provision(Maritime(SecretGuard()), _opts(tmp_path, [], judge=True))
    judge_env_file = (tmp_path / ".approved" / "acme" / "acme-judge.env").read_text()
    assert f"JUDGE_TG_BOT_TOKEN={JUDGE_BOT}" in judge_env_file
    assert BOT_TOKEN not in judge_env_file
    assert BOT_TOKEN.split(":")[1] not in judge_env_file
    assert "HOSTED_ACME_TG_BOT_TOKEN" not in judge_env_file


def test_judge_without_its_own_bot_is_refused_before_any_call(
    fake: FakeMaritime, tmp_path: Path
) -> None:
    with pytest.raises(ProvisionError, match="own bot"):
        provision(
            Maritime(SecretGuard()), _opts(tmp_path, [], judge=True, judge_bot_token_file=None)
        )
    assert fake.calls() == []


def test_judge_bot_equal_to_the_gate_bot_is_refused(fake: FakeMaritime, tmp_path: Path) -> None:
    same = tmp_path / "same-bot"
    same.write_text(BOT_TOKEN)
    lines: list[str] = []
    with pytest.raises(ProvisionError, match="approval bot's token") as info:
        provision(
            Maritime(SecretGuard()), _opts(tmp_path, lines, judge=True, judge_bot_token_file=same)
        )
    assert BOT_TOKEN not in str(info.value)
    assert fake.calls() == []


def test_bundle_judge_env_has_no_gate_bot_reference() -> None:
    from approved.console.bundle import judge_env

    refs = {v.ref for v in judge_env("acme", "https://f.test")}
    assert "tg_bot_token" not in refs
    assert "judge_bot_token" in refs
    assert not any(v.name == "TG_BOT_TOKEN" for v in judge_env("acme", "https://f.test"))


# ------------------------------------------------------------------ B2: never rewrite silently


def _reused_with_other_policy(fake: FakeMaritime, tmp_path: Path) -> None:
    provision(Maritime(SecretGuard()), _opts(tmp_path, []))
    fake.set(remote_policy_sha="f" * 64)  # someone attested a different policy since


def _policy_writes(fake: FakeMaritime) -> int:
    return sum("base64 -d" in json.dumps(a) for a in fake.argvs("exec"))


def test_reused_daemon_policy_is_never_rewritten(fake: FakeMaritime, tmp_path: Path) -> None:
    _reused_with_other_policy(fake, tmp_path)
    before = _policy_writes(fake)
    with pytest.raises(ProvisionError, match="different policy") as info:
        provision(Maritime(SecretGuard()), _opts(tmp_path, []))
    assert "f" * 64 in str(info.value)
    assert "--replace-policy" in str(info.value)
    assert _policy_writes(fake) == before


def test_replace_policy_refuses_while_a_request_is_open(fake: FakeMaritime, tmp_path: Path) -> None:
    _reused_with_other_policy(fake, tmp_path)
    fake.set(queue={"ok": True, "pending": [{"action_key": "k"}]})
    before = _policy_writes(fake)
    with pytest.raises(ProvisionError, match="1 open request"):
        provision(Maritime(SecretGuard()), _opts(tmp_path, [], replace_policy=True))
    assert _policy_writes(fake) == before


def test_replace_policy_refuses_when_the_queue_is_unreadable(
    fake: FakeMaritime, tmp_path: Path
) -> None:
    _reused_with_other_policy(fake, tmp_path)
    fake.set(queue="not a queue")
    with pytest.raises(ProvisionError, match="could not be read"):
        provision(Maritime(SecretGuard()), _opts(tmp_path, [], replace_policy=True))


def test_replace_policy_writes_and_asks_for_reattestation(
    fake: FakeMaritime, tmp_path: Path
) -> None:
    _reused_with_other_policy(fake, tmp_path)
    lines: list[str] = []
    provision(Maritime(SecretGuard()), _opts(tmp_path, lines, replace_policy=True))
    assert any("re-)attested" in line for line in lines)
    assert fake.state["remote_policy_sha"] != "f" * 64


# ------------------------------------------------------------------ S6: resolved names


@pytest.mark.parametrize("typed", ["  Approval-Hermes-Gated ", "APPROVAL-X16-HERMES", "My-DogFood"])
def test_protected_names_are_normalised(typed: str) -> None:
    assert is_protected(typed)


def test_an_id_resolving_to_a_protected_machine_is_refused(fake: FakeMaritime, capsys) -> None:
    fake.set(agents=[{"id": "ag-7f3", "name": "approval-hermes-gated"}])
    assert main(["ask", "ag-7f3", "hello"]) == 2
    assert "approval-hermes-gated" in capsys.readouterr().out
    assert fake.argvs("exec") == []  # resolved, refused, nothing run


def test_a_configured_protected_id_is_refused(
    fake: FakeMaritime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPROVED_PROTECTED_AGENT_IDS", "ag-dog-1")
    fake.set(agents=[{"id": "ag-dog-1", "name": "innocent-looking"}])
    assert main(["ask", "innocent-looking", "hello"]) == 2
    assert fake.argvs("exec") == []
