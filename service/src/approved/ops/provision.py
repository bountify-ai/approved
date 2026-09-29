"""``approved provision``: stand up a tenant's machines on Maritime, idempotently.

Order, for each machine (daemon, then optional gated Hermes, then optional judge):

1. **Reuse or create.** ``maritime list`` finds an existing machine by name; it is reused,
   never recreated. A machine that exists while ``./.approved/<tenant>/`` holds no
   credentials is refused: its running pair would not match anything this CLI could import.
2. **Create** with ``create_argv`` (the same spec the console's bundle prints) and only
   non-secret ``-e`` pairs, so they are in place before the first boot.
3. **Import** the credentials from a 0600 file (``maritime env import <agent> <file>``),
   then **stop and start** once, because env set after boot reaches the processes only
   after a restart. Only on a fresh create: no request can be open on a machine that has
   never had its credentials.

Then, for the daemon: wait for the public ``/health`` (200) and an unauthenticated
``/status`` (401: the facade is up and refusing strangers), write the policy bytes into the
store over ``maritime exec`` (base64 on argv: a policy is not a secret) and verify its
sha256 remotely, and print the attest command for the human. **This CLI never attests.**
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from ..console.bundle import (
    EnvVar,
    create_argv,
    daemon_env,
    env_prefix,
    hermes_env,
    judge_env,
    public_url,
    validate_approver,
    validate_tenant,
)
from .credentials import CredentialDir
from .maritime import Maritime, MaritimeError, agent_id
from .names import identities, refuse_any_protected, refuse_protected

__all__ = [
    "APPROVAL_CLI",
    "ProvisionError",
    "ProvisionOptions",
    "attest_command",
    "provision",
]

APPROVAL_CLI = "/opt/runtime/node_modules/approval-md/cli.js"
MAX_POLICY_BYTES = 48 * 1024
_SHA_LINE = re.compile(r"^([0-9a-f]{64})\s")


class ProvisionError(RuntimeError):
    pass


@dataclass
class ProvisionOptions:
    tenant: str
    policy: Path
    approver: str = "operator"
    tg_chat: str | None = None
    tg_bot_token_file: Path | None = None
    judge_bot_token_file: Path | None = None
    replace_policy: bool = False
    hermes: bool = False
    hermes_model: str | None = None
    hermes_provider: str | None = None
    hermes_base_url: str | None = None
    hermes_key_env: str | None = None
    judge: bool = False
    credentials_root: Path = Path(".approved")
    wait_s: float = 300.0
    poll_s: float = 5.0
    out: Callable[[str], None] = print
    http: httpx.Client | None = None
    sleep: Callable[[float], None] = time.sleep
    created: list[str] = field(default_factory=list)


def attest_command(daemon: str, tenant: str, approver: str) -> str:
    """The command a HUMAN runs to attest the policy. Printed, never executed here."""
    inner = f"cd /data/{tenant} && node {APPROVAL_CLI} policy attest --as human:{approver} --json"
    return f"maritime exec --json {daemon} -- sh -c '{inner}'"


def public_url_of(agent: dict[str, Any]) -> str:
    for key in ("publicUrl", "public_url", "url"):
        value = agent.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value.rstrip("/")
    ident = agent_id(agent)
    if not ident:
        raise ProvisionError("maritime did not report the daemon's id; cannot derive its URL")
    return public_url(ident)


def _create_env(variables: list[EnvVar], exclude: frozenset[str]) -> list[str]:
    """Non-secret, known values as ``-e`` pairs: in place before the first boot."""
    pairs: list[str] = []
    for var in variables:
        if var.ref is None and not var.placeholder and var.name not in exclude:
            pairs += ["-e", f"{var.name}={var.value}"]
    return pairs


ImportFn = Callable[[dict[str, Any]], list[EnvVar]]


class _Provisioner:
    def __init__(self, maritime: Maritime, opts: ProvisionOptions, creds: CredentialDir) -> None:
        self.m = maritime
        self.o = opts
        self.creds = creds
        self.http = opts.http or httpx.Client(timeout=10.0, follow_redirects=False)

    def say(self, line: str) -> None:
        self.o.out(self.m.guard.redact(line))

    # -------------------------------------------------------------- one machine

    def ensure_machine(
        self,
        kind: Literal["daemon", "hermes", "judge"],
        name: str,
        create_vars: list[EnvVar],
        import_vars: ImportFn,
        *,
        existing: dict[str, Any] | None,
        exclude_from_create: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        if existing is not None:
            self.say(f"{name}: exists ({agent_id(existing) or '?'}), reusing; not recreated")
            return existing
        argv = create_argv(kind, name) + _create_env(create_vars, exclude_from_create)
        self.say(f"{name}: creating ({' '.join(argv[:3])} ...)")
        created = self.m.create(argv)
        self.o.created.append(name)
        if identities(created):  # whatever create says it made must not be protected
            refuse_any_protected(created, what=name)
        nested = created.get("agent")
        agent: dict[str, Any] = nested if isinstance(nested, dict) else created
        found = self.m.find(name) or agent
        env_path = self.creds.env_file(name, import_vars(found))
        if env_path is not None:
            self.say(f"{name}: importing credentials from {env_path} (0600)")
            self.m.env_import(name, str(env_path))
        self.say(f"{name}: stop, then start, so the imported env reaches the processes")
        self.m.stop(name)
        self.m.start(name)
        return found

    # -------------------------------------------------------------- daemon checks

    def wait_for_facade(self, base: str) -> None:
        deadline = time.monotonic() + self.o.wait_s
        health_ok = status_ok = False
        while time.monotonic() < deadline:
            try:
                health_ok = health_ok or self.http.get(f"{base}/health").status_code == 200
                status_ok = status_ok or self.http.get(f"{base}/status").status_code == 401
            except httpx.HTTPError:
                pass
            if health_ok and status_ok:
                self.say(f"facade up: /health 200, unauthenticated /status 401 ({base})")
                return
            self.o.sleep(self.o.poll_s)
        raise ProvisionError(
            f"facade not ready within {int(self.o.wait_s)}s "
            f"(/health 200: {health_ok}, /status 401: {status_ok})"
        )

    def probe_policy(self, daemon: str, tenant: str) -> str | None:
        """The store policy's sha256, ``None`` if there is no policy file. Anything else (an
        exec failure, an unreadable file, output that does not parse) raises: never a guess."""
        store = f"/data/{tenant}"
        probe = self.m.exec(
            daemon,
            f"if [ -f {store}/APPROVAL.md ]; then sha256sum {store}/APPROVAL.md || echo UNREADABLE;"
            " else echo ABSENT; fi\n",
        )
        text = probe.stdout.strip()
        if probe.exit_code == 0 and text == "ABSENT":
            return None
        match = _SHA_LINE.match(text)
        if probe.exit_code != 0 or match is None:
            raise ProvisionError(
                f"could not read {daemon}'s current policy hash; nothing was written"
            )
        return match.group(1)

    def write_policy(
        self, daemon: str, tenant: str, policy: bytes, *, reused: bool, replace: bool
    ) -> tuple[str, bool]:
        """Put the policy in the store. Returns (sha256, written).

        On a REUSED daemon an existing, different policy is never overwritten unless
        ``--replace-policy`` is given, and then the open-request check and the write run in ONE
        exec script (``approval queue --json`` must show nothing pending, or nothing is
        written), so no request can open between the check and the write. After the write the
        policy is unattested until the human re-attests, and core runs an unattested policy
        manual-only: the window fails safe, it does not fail open.
        """
        local_sha = hashlib.sha256(policy).hexdigest()
        remote_sha = self.probe_policy(daemon, tenant)
        if remote_sha == local_sha:
            self.say(f"policy already in place (sha256 {local_sha[:12]}...)")
            return local_sha, False
        check_queue = reused and remote_sha is not None
        if check_queue and not replace:
            raise ProvisionError(
                f"{daemon} already holds a different policy (remote sha256 {remote_sha}, "
                f"local {local_sha}). Nothing was written. To replace it, confirm no "
                "approval request is open and re-run with --replace-policy; the approver "
                "must then re-attest."
            )
        encoded = base64.b64encode(policy).decode("ascii")
        store = f"/data/{tenant}"
        guard = (
            'q="$(node "$APPROVAL_CLI" queue --json)" || { echo QUEUE-UNREADABLE >&2; exit 5; }\n'
            "node -e 'const q=JSON.parse(process.argv[1]);"
            'process.exit(Array.isArray(q.pending)&&q.pending.length===0?0:4)\' "$q" '
            "|| { echo OPEN-REQUESTS >&2; exit 4; }\n"
            if check_queue
            else ""
        )
        script = (
            "set -eu\n"
            f"d={store}\n"
            'test -d "$d" || { echo "store $d missing: has the daemon booted?" >&2; exit 3; }\n'
            'cd "$d"\n'
            + guard
            + f"printf %s '{encoded}' | base64 -d > \"$d/.APPROVAL.md.approved-tmp\"\n"
            'mv "$d/.APPROVAL.md.approved-tmp" "$d/APPROVAL.md"\n'
            'sha256sum "$d/APPROVAL.md"\n'
        )
        result = self.m.exec(daemon, script)
        if result.exit_code == 4:
            raise ProvisionError(
                f"not replacing the policy: {daemon} has open approval requests. Decide them or "
                "let them expire first. Nothing was written."
            )
        if result.exit_code == 5:
            raise ProvisionError(
                f"not replacing the policy: {daemon}'s queue could not be read. Nothing was "
                "written."
            )
        remote = _SHA_LINE.match(result.stdout.strip())
        if result.exit_code != 0 or remote is None:
            raise ProvisionError(f"writing the policy failed (exit {result.exit_code})")
        if remote.group(1) != local_sha:
            raise ProvisionError("the policy's remote sha256 does not match the local file")
        self.say(f"policy written and verified remotely (sha256 {local_sha[:12]}...)")
        return local_sha, True


def provision(maritime: Maritime, opts: ProvisionOptions) -> dict[str, Any]:
    tenant = validate_tenant(opts.tenant)
    approver = validate_approver(opts.approver)
    daemon, hermes, judge = f"{tenant}-daemon", f"{tenant}-hermes", f"{tenant}-judge"
    for name in (tenant, daemon, hermes, judge):
        refuse_protected(name)
    policy = opts.policy.read_bytes()
    if not policy.strip() or len(policy) > MAX_POLICY_BYTES:
        raise ProvisionError("the policy file is empty or larger than 48 KiB")

    judge_bot: str | None = None
    if opts.judge:
        # The judge posts through its OWN bot. It must never hold the approval bot's token:
        # that token can send prompts and, with the webhook, stands for the gate's channel.
        if opts.judge_bot_token_file is None:
            raise ProvisionError(
                "--judge needs --judge-bot-token-file: the judge posts through its own bot "
                "(create a second bot with @BotFather; the approver must /start it)"
            )
        judge_bot = opts.judge_bot_token_file.read_text(encoding="utf-8").strip()
        if not judge_bot:
            raise ProvisionError("--judge-bot-token-file is empty")

    creds = CredentialDir(opts.credentials_root, tenant)
    gate_bot = (
        opts.tg_bot_token_file.read_text(encoding="utf-8").strip()
        if opts.tg_bot_token_file is not None
        else (creds.get("tg_bot_token") if creds.path.exists() else None)
    )
    if judge_bot is not None and gate_bot is not None and judge_bot == gate_bot:
        raise ProvisionError(
            "the judge bot token file holds the approval bot's token; the judge needs its own "
            "bot (compared locally; neither value is printed)"
        )
    agents = {a.get("name"): a for a in maritime.list_agents()}
    for name in (daemon, hermes, judge):
        found = agents.get(name)
        if found is not None:  # reuse only what does not resolve to a protected machine
            refuse_any_protected(found, what=name)
    wanted = [daemon] + ([hermes] if opts.hermes else []) + ([judge] if opts.judge else [])
    if any(n in agents for n in wanted) and not creds.exists:
        raise ProvisionError(
            f"a machine for {tenant} already exists but {creds.path} holds no credentials; "
            "refusing to reuse a machine whose credentials this CLI does not have"
        )
    creds.ensure()
    if opts.tg_bot_token_file is not None and gate_bot:
        creds.store("tg_bot_token", gate_bot)
    if judge_bot is not None:
        creds.store("judge_bot_token", judge_bot)
    maritime.guard.add(*creds.values())
    p = _Provisioner(maritime, opts, creds)

    # The daemon. Its public URL depends on its id, so it goes in with the credentials.
    has_bot = creds.get("tg_bot_token") is not None
    d_vars = daemon_env(tenant, "https://placeholder.invalid", approver, opts.tg_chat)
    if not has_bot:  # no bot token: no webhook mode (the channel is skipped, the gate runs)
        d_vars = [v for v in d_vars if v.ref != "webhook_secret"]

    def daemon_imports(agent: dict[str, Any]) -> list[EnvVar]:
        secrets_only = [v for v in d_vars if v.ref is not None]
        # The public URL is known only once the machine exists; it goes in with the
        # credentials, before the one restart. Webhook mode needs it.
        return [*secrets_only, EnvVar("APPROVAL_PUBLIC_URL", public_url_of(agent))]

    d_agent = p.ensure_machine(
        "daemon",
        daemon,
        d_vars,
        daemon_imports if has_bot else (lambda _a: [v for v in d_vars if v.ref is not None]),
        existing=agents.get(daemon),
        exclude_from_create=frozenset({"APPROVAL_PUBLIC_URL"}),
    )
    base = public_url_of(d_agent)

    if opts.hermes:
        h_vars = hermes_env(
            tenant,
            base,
            model=opts.hermes_model,
            provider=opts.hermes_provider,
            base_url=opts.hermes_base_url,
            key_env=opts.hermes_key_env,
        )
        h_secret = [v for v in h_vars if v.ref is not None]
        p.ensure_machine("hermes", hermes, h_vars, lambda _a: h_secret, existing=agents.get(hermes))
    if opts.judge:
        j_vars = judge_env(tenant, base, opts.tg_chat)
        j_secret = [v for v in j_vars if v.ref is not None]

        def judge_imports(agent: dict[str, Any]) -> list[EnvVar]:
            ident = agent_id(agent)
            base_path = [EnvVar("PUBLIC_BASE_PATH", f"/a/{ident}")] if ident else []
            return [*j_secret, *base_path]

        p.ensure_machine("judge", judge, j_vars, judge_imports, existing=agents.get(judge))

    p.wait_for_facade(base)
    try:
        sha, written = p.write_policy(
            daemon,
            tenant,
            policy,
            reused=daemon not in opts.created,
            replace=opts.replace_policy,
        )
    except MaritimeError as exc:
        raise ProvisionError(f"policy write over maritime exec failed: {exc}") from None

    p.say("")
    if written:
        p.say("The policy changed, so it must be (re-)attested before the gate uses it.")
    p.say("Next, as the human approver (this CLI never attests):")
    p.say(f"  {attest_command(daemon, tenant, approver)}")
    p.say(f"Credentials: {creds.path}/ (0600). Tenant env prefix: {env_prefix(tenant)}")
    return {"daemon": daemon, "facade_url": base, "policy_sha256": sha, "created": opts.created}
