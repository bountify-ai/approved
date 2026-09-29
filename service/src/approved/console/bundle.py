"""Tenant wiring, defined once: the environment each machine needs, and the connect bundle.

Both the console's ``/connect`` page and the ``approved provision`` / ``approved connect``
CLI read the specs below (:func:`daemon_env`, :func:`hermes_env`, :func:`hook_env`,
:func:`judge_env`, :func:`create_argv`), so the page and the CLI cannot disagree about a
variable name, a port or a repository.

Two bundle modes:

* ``maritime``: "Approved agent on Maritime". The daemon image (approval-md-hosted root
  ``Dockerfile``, public URL) and the gated Hermes image (``bountify-ai/approval-hermes-image``),
  each created with ``maritime create`` and configured with ``maritime env import``.
* ``byo``: "Bring your own Hermes". The daemon on Maritime as above, plus the Hermes hook
  shim and a ``pre_tool_call`` hooks block for a Hermes you run yourself.

Env names come from the daemon image's README ("Environment", vendored in
``images/daemon/README.md``) and approval-md-hosted ``hermes-image/README.md`` "Environment";
the hooks block follows approval-md ``docs/hermes-hook.md`` "Installing it". Platform facts
(approval-md-hosted ``docs/02-hosted-daemon-architecture.md`` sections 4.3 and 4.6): the
injected port is 18789, the health path is ``/health``, a public URL is a create-time setting
only, a private repository needs an explicit ``custom`` framework (never the ``hermes``
template, which Maritime regenerates on every boot), and env set after boot reaches the
processes only after a restart (``maritime stop`` then ``maritime start``, never while a
request is open: a stop loses unflushed writes).

**No credential is generated, shown or sent by this module.** Secret variables are
*references* (:class:`EnvVar` with ``ref`` set): the bundle renders them as shell
variables its script fills from ``openssl rand``, and ``approved provision`` fills them from
mode-0600 files. Values a person must supply appear only as ``<placeholders>``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "DAEMON_REPO",
    "GENERATED_SECRETS",
    "HERMES_REPO",
    "HOOKS_YAML",
    "JUDGE_REPO",
    "MARITIME_PORT",
    "SHIM_SHA256",
    "BundleError",
    "ConnectBundle",
    "EnvVar",
    "Mode",
    "SecretRef",
    "build_bundle",
    "create_argv",
    "daemon_env",
    "env_prefix",
    "hermes_env",
    "hook_env",
    "judge_env",
    "public_url",
    "validate_approver",
    "validate_tenant",
    "validate_url",
]

Mode = Literal["maritime", "byo"]

DAEMON_REPO = "https://github.com/bountify-ai/approval-md-hosted"
HERMES_REPO = "https://github.com/bountify-ai/approval-hermes-image"
JUDGE_REPO = "https://github.com/bountify-ai/approved"
MARITIME_PORT = 18789
MARITIME_PUBLIC_BASE = "https://api.maritime.sh/a/"
SHIM_PATH = "/downloads/hermes-hook-shim.sh"
SHIM_SHA256 = "b577532ac05689a67a47c4340f4f7846dc67b079c527859539954694874527bd"

#: Secret references. The bundle's script defines a shell variable of each name; provision
#: keeps a 0600 file per tenant holding each value.
SecretRef = Literal["agent", "tenant_token", "webhook_secret", "console_token", "tg_bot_token"]
GENERATED_SECRETS: tuple[SecretRef, ...] = (
    "agent",
    "tenant_token",
    "webhook_secret",
    "console_token",
)

HOOKS_YAML = """plugins:
  hook_callback_timeout: 600
hooks_auto_accept: true
hooks:
  pre_tool_call:
    - command: /usr/local/bin/hermes-hook-shim.sh
      timeout: 300
      fail_closed: true"""

_TENANT = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
_APPROVER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_URL = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{2,5})?(?:/[A-Za-z0-9._~/-]*)?$")


class BundleError(ValueError):
    """An input the bundle will not render. The message names the field only."""


@dataclass(frozen=True)
class EnvVar:
    """One machine variable. ``ref`` names a secret reference instead of a value."""

    name: str
    value: str = ""
    ref: SecretRef | None = None
    placeholder: bool = False  # value is a <placeholder> a person must replace


@dataclass(frozen=True)
class ConnectBundle:
    mode: Mode
    tenant: str
    title: str
    script: str
    hooks_yaml: str | None
    notes: list[str]


# ---------------------------------------------------------------- validation


def validate_tenant(tenant: str) -> str:
    tenant = tenant.strip()
    if not _TENANT.match(tenant):
        raise BundleError("tenant: 3-32 characters, lowercase letters, digits and hyphens")
    return tenant


def validate_approver(approver: str) -> str:
    approver = approver.strip()
    if not _APPROVER.match(approver):
        raise BundleError("approver: lowercase letters, digits, '.', '_' or '-'")
    return approver


def validate_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not _URL.match(url):
        raise BundleError("facade URL: an https:// base URL with no query or fragment")
    return url


def env_prefix(tenant: str) -> str:
    """``HOSTED_<TENANT>_``: tenant-prefixed names, as the daemon image requires."""
    return "HOSTED_" + tenant.upper().replace("-", "_") + "_"


def public_url(agent_id: str) -> str:
    """A public Maritime agent's base URL (daemon image README, ``APPROVAL_PUBLIC_URL``)."""
    return MARITIME_PUBLIC_BASE + agent_id


# ---------------------------------------------------------------- env specs


def daemon_env(
    tenant: str, facade_url: str, approver: str, tg_chat: str | None = None
) -> list[EnvVar]:
    p = env_prefix(tenant)
    return [
        EnvVar("APPROVAL_TENANT", tenant),
        EnvVar("APPROVAL_AGENT", f"agent:hermes-{tenant}"),
        EnvVar("APPROVAL_DAEMON_ID", f"daemon-{tenant}-1"),
        EnvVar("APPROVAL_HUMAN", f"human:{approver}"),
        EnvVar("APPROVAL_SERVE_HOOK_HARNESS_CAP", "300s"),
        EnvVar("APPROVAL_SERVE_HOOK_TIMEOUT", "12s"),
        EnvVar("APPROVAL_PUBLIC_URL", facade_url),
        EnvVar(f"{p}TG_CHAT", tg_chat or "<your Telegram user id>", placeholder=tg_chat is None),
        EnvVar("APPROVAL_SERVE_AGENT_TOKEN", ref="agent"),
        EnvVar("APPROVAL_SERVE_TENANT_TOKEN", ref="tenant_token"),
        EnvVar("APPROVAL_TG_WEBHOOK_SECRET", ref="webhook_secret"),
        EnvVar(f"{p}TG_BOT_TOKEN", ref="tg_bot_token"),
    ]


def hermes_env(
    tenant: str,
    facade_url: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    key_env: str | None = None,
) -> list[EnvVar]:
    """The gated Hermes machine: the AGENT credential only, under tenant-prefixed names."""
    p = env_prefix(tenant)

    def opt(name: str, value: str | None, hint: str) -> EnvVar:
        return EnvVar(name, value or hint, placeholder=value is None)

    return [
        EnvVar("APPROVAL_FACADE_URL_ENV", f"{p}FACADE_URL"),
        EnvVar("APPROVAL_FACADE_TOKEN_ENV", f"{p}FACADE_AGENT_TOKEN"),
        EnvVar(f"{p}FACADE_URL", facade_url),
        opt("APPROVAL_HERMES_MODEL", model, "<model, e.g. gpt-5.4>"),
        opt("APPROVAL_HERMES_PROVIDER", provider, "openai"),
        opt("APPROVAL_HERMES_BASE_URL", base_url, "https://api.maritime.sh/api/llm/v1"),
        opt("APPROVAL_HERMES_KEY_ENV", key_env, "OPENAI_API_KEY"),
        EnvVar(f"{p}FACADE_AGENT_TOKEN", ref="agent"),
    ]


def hook_env(tenant: str, facade_url: str) -> list[EnvVar]:
    """A Hermes you run yourself: what the hook shim reads from its environment."""
    p = env_prefix(tenant)
    return [
        EnvVar("APPROVAL_HOOK_URL_ENV", f"{p}FACADE_URL"),
        EnvVar("APPROVAL_HOOK_TOKEN_ENV", f"{p}FACADE_AGENT_TOKEN"),
        EnvVar("APPROVAL_HOOK_WAIT_S", "240"),
        EnvVar("APPROVAL_HOOK_LOG", "/tmp/approval-hook.log"),  # noqa: S108 - the shim's log path
        EnvVar(f"{p}FACADE_URL", facade_url),
        EnvVar(f"{p}FACADE_AGENT_TOKEN", ref="agent"),
    ]


def judge_env(tenant: str, facade_url: str, tg_chat: str | None = None) -> list[EnvVar]:
    """The Approved judge and console: the TENANT credential only, never the agent's."""
    return [
        EnvVar("FACADE_URL", facade_url),
        EnvVar("STATE_DIR", f"/data/judge-{tenant}"),
        EnvVar("OFFLINE", "1"),
        EnvVar("TG_CHAT_ID", tg_chat or "<your Telegram user id>", placeholder=tg_chat is None),
        EnvVar("TENANT_TOKEN", ref="tenant_token"),
        EnvVar("CONSOLE_TOKEN", ref="console_token"),
        EnvVar("TG_BOT_TOKEN", ref="tg_bot_token"),
    ]


def create_argv(kind: Literal["daemon", "hermes", "judge"], agent: str) -> list[str]:
    """``maritime create`` for one machine, without env (the caller adds ``-e`` pairs).

    ``--framework custom`` rather than a template: a private repository needs an explicit
    framework, and the ``hermes`` template is regenerated by Maritime on every boot.
    """
    repo = {"daemon": DAEMON_REPO, "hermes": HERMES_REPO, "judge": JUDGE_REPO}[kind]
    argv = ["maritime", "create", agent, "--repo", repo, "--branch", "main"]
    argv += ["--framework", "custom"]
    if kind in ("daemon", "judge"):
        argv += ["--public", "--port", str(MARITIME_PORT)]
    return argv


# ---------------------------------------------------------------- bundle rendering


def _shell_value(var: EnvVar) -> str:
    if var.ref == "tg_bot_token":
        return "<your approval bot token, from @BotFather>"
    if var.ref is not None:
        return "${" + var.ref + "}"
    return var.value


def _env_file(path: str, variables: list[EnvVar]) -> str:
    body = "\n".join(f"{v.name}={_shell_value(v)}" for v in variables)
    return f'cat > "$dir/{path}" <<EOF\n{body}\nEOF'


def _script(tenant: str, facade_url: str, approver: str, byo: bool) -> str:
    daemon_create = " ".join(create_argv("daemon", f"{tenant}-daemon"))
    hermes_create = " ".join(create_argv("hermes", f"{tenant}-hermes"))
    judge_vars = [v for v in judge_env(tenant, facade_url) if v.ref != "tg_bot_token"]
    judge_vars.append(EnvVar("TG_BOT_TOKEN", "<the judge's bot token>", placeholder=True))
    harness = (
        "# The AGENT credential for YOUR Hermes. Put these lines in the environment Hermes\n"
        "# runs with (for example $HERMES_HOME/.env, which the gate treats as a credential).\n"
        + _env_file("hermes-hook.env", hook_env(tenant, facade_url))
        if byo
        else "# The gated Hermes machine: the AGENT credential only.\n"
        + _env_file("hermes.env", hermes_env(tenant, facade_url))
        + "\n# (add OPENAI_API_KEY=<your model provider key>, or the variable"
        " APPROVAL_HERMES_KEY_ENV names)"
    )
    steps = f"""
# Create the daemon (public: Telegram and your harness reach it), then its environment.
{daemon_create}
maritime env import {tenant}-daemon "$dir/daemon.env"
"""
    if not byo:
        steps += f"""
# Create the gated Hermes machine (no public URL: the hook calls out), then its env.
{hermes_create}
maritime env import {tenant}-hermes "$dir/hermes.env"
"""
    steps += f"""
# Env set after boot reaches the processes only after a restart. Stop then start each
# machine once, while no approval request is open (a stop loses unflushed writes):
maritime stop {tenant}-daemon && maritime start {tenant}-daemon
"""
    if not byo:
        steps += f"maritime stop {tenant}-hermes && maritime start {tenant}-hermes\n"
    return f"""#!/usr/bin/env bash
# Approved connect bundle for tenant "{tenant}". Run on YOUR machine. It generates fresh
# credentials into mode-0600 files under ./approved-{tenant}/; nothing is sent anywhere
# until you run the maritime commands below yourself.
set -euo pipefail
umask 077
dir="approved-{tenant}"
mkdir -p "$dir"
rand() {{ openssl rand -hex 24; }}
agent="$(rand)"          # the AGENT credential: the harness's hook only
tenant_token="$(rand)"   # the TENANT credential: the log, export, status (and the judge)
webhook_secret="$(rand)" # Telegram's secret_token for the daemon's webhook
console_token="$(rand)"  # the Approved console's sign-in token

# The daemon machine (approval up + approval serve + the Telegram webhook).
{_env_file("daemon.env", daemon_env(tenant, facade_url, approver))}

{harness}

# The Approved judge and console: the TENANT credential only. Never the agent credential.
{_env_file("judge.env", judge_vars)}
chmod 600 "$dir"/*.env
unset agent tenant_token webhook_secret console_token
echo "credentials written to $dir/ (0600). Fill in the <placeholders> before importing."
{steps}"""


def build_bundle(
    *, mode: Mode, tenant: str, facade_url: str, approver: str = "operator"
) -> ConnectBundle:
    tenant = validate_tenant(tenant)
    facade_url = validate_url(facade_url)
    approver = validate_approver(approver)
    byo = mode == "byo"
    if byo:
        hooks_yaml: str | None = HOOKS_YAML
        notes = [
            f"Download the hook shim from this console ({SHIM_PATH}), check its sha256 is "
            f"{SHIM_SHA256}, and install it as /usr/local/bin/hermes-hook-shim.sh (root-owned, "
            "0755).",
            "Add the hooks block to $HERMES_HOME/config.yaml, and spell the last segment of "
            "HERMES_HOME .hermes so the gate recognises its files (docs/hermes-hook.md).",
            "Load hermes-hook.env into Hermes's environment. The shim reads the facade URL and "
            "the AGENT credential from the variables it names, and never logs them.",
            "The shim fails closed: an unreachable facade blocks the tool call.",
        ]
        title = "Bring your own Hermes"
    else:
        hooks_yaml = None
        notes = [
            "The gated Hermes image writes its own hooks and consent, and refuses to start "
            "unless its self-check passes.",
        ]
        title = "Approved agent on Maritime"
    notes += [
        "Env set after boot reaches running processes only after a restart: maritime stop, "
        "then maritime start, while no request is open.",
        "Attest the tenant's APPROVAL.md as the human approver before the gate will decide "
        "anything (approval policy attest --as human:<you>). Nothing here does this for you.",
        "Or let the CLI do all of this: approved provision <tenant> --policy APPROVAL.md.",
    ]
    return ConnectBundle(
        mode=mode,
        tenant=tenant,
        title=title,
        script=_script(tenant, facade_url, approver, byo),
        hooks_yaml=hooks_yaml,
        notes=notes,
    )
