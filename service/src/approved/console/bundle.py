"""The connect bundle: the exact commands to stand up a gated tenant, with no secret in them.

Two modes:

* ``maritime``: "Approved agent on Maritime". The daemon image (approval-md-hosted root
  ``Dockerfile``, public URL) and the gated Hermes image (``bountify-ai/approval-hermes-image``),
  each created with ``maritime create`` and configured with ``maritime env import``.
* ``byo``: "Bring your own Hermes". The daemon on Maritime as above, plus the Hermes hook
  shim and a ``pre_tool_call`` hooks block for a Hermes you run yourself.

Env names come from the daemon image's README ("Environment", vendored in
``images/daemon/README.md``) and approval-md-hosted ``hermes-image/README.md`` "Environment";
the hooks block follows approval-md ``docs/hermes-hook.md`` "Installing it"; the ``maritime``
commands follow ``maritime create --help`` / ``maritime env import --help`` (CLI 1.7.0).

**No credential is generated, shown or sent here.** The bundle is text: a shell script the
operator runs on their own machine, which generates the credentials with ``openssl rand``
into mode-0600 files (as ``demo/setup.sh`` does) and hands them to ``maritime env import``.
Values a person must supply (the bot token, their Telegram id, the model key) appear only as
``<placeholders>``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

__all__ = ["BundleError", "ConnectBundle", "Mode", "build_bundle", "env_prefix"]

Mode = Literal["maritime", "byo"]

DAEMON_REPO = "https://github.com/bountify-ai/approval-md-hosted"
HERMES_REPO = "https://github.com/bountify-ai/approval-hermes-image"
SHIM_PATH = "/downloads/hermes-hook-shim.sh"
SHIM_SHA256 = "b577532ac05689a67a47c4340f4f7846dc67b079c527859539954694874527bd"

_TENANT = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
_APPROVER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_URL = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{2,5})?(?:/[A-Za-z0-9._~/-]*)?$")


class BundleError(ValueError):
    """An input the bundle will not render. The message names the field only."""


@dataclass(frozen=True)
class ConnectBundle:
    mode: Mode
    tenant: str
    title: str
    script: str
    hooks_yaml: str | None
    notes: list[str]


def env_prefix(tenant: str) -> str:
    """``HOSTED_<TENANT>_``: tenant-prefixed names, as the daemon image requires."""
    return "HOSTED_" + tenant.upper().replace("-", "_") + "_"


def _validate(tenant: str, facade_url: str, approver: str) -> None:
    if not _TENANT.match(tenant):
        raise BundleError("tenant: 3-32 characters, lowercase letters, digits and hyphens")
    if not _URL.match(facade_url):
        raise BundleError("facade URL: an https:// base URL with no query or fragment")
    if not _APPROVER.match(approver):
        raise BundleError("approver: lowercase letters, digits, '.', '_' or '-'")


def _credentials_block(tenant: str, facade_url: str, approver: str, byo: bool) -> str:
    p = env_prefix(tenant)
    hermes_env = (
        f"""# The AGENT credential for YOUR Hermes. Put these lines in the environment Hermes
# runs with (for example $HERMES_HOME/.env, which the gate treats as a credential file).
cat > "$dir/hermes-hook.env" <<EOF
APPROVAL_HOOK_URL_ENV={p}FACADE_URL
APPROVAL_HOOK_TOKEN_ENV={p}FACADE_AGENT_TOKEN
APPROVAL_HOOK_WAIT_S=240
APPROVAL_HOOK_LOG=/tmp/approval-hook.log
{p}FACADE_URL={facade_url}
{p}FACADE_AGENT_TOKEN=${{agent}}
EOF"""
        if byo
        else f"""# The gated Hermes machine: the AGENT credential only, under tenant-prefixed names.
cat > "$dir/hermes.env" <<EOF
APPROVAL_FACADE_URL_ENV={p}FACADE_URL
APPROVAL_FACADE_TOKEN_ENV={p}FACADE_AGENT_TOKEN
{p}FACADE_URL={facade_url}
{p}FACADE_AGENT_TOKEN=${{agent}}
APPROVAL_HERMES_MODEL=<model, e.g. gpt-5.4>
APPROVAL_HERMES_PROVIDER=openai
APPROVAL_HERMES_BASE_URL=https://api.maritime.sh/api/llm/v1
APPROVAL_HERMES_KEY_ENV=OPENAI_API_KEY
OPENAI_API_KEY=<your model provider key>
EOF"""
    )
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

# The daemon machine (approval up + approval serve + the Telegram webhook).
cat > "$dir/daemon.env" <<EOF
APPROVAL_TENANT={tenant}
APPROVAL_AGENT=agent:hermes-{tenant}
APPROVAL_DAEMON_ID=daemon-{tenant}-1
APPROVAL_HUMAN=human:{approver}
APPROVAL_SERVE_AGENT_TOKEN=${{agent}}
APPROVAL_SERVE_TENANT_TOKEN=${{tenant_token}}
APPROVAL_SERVE_HOOK_HARNESS_CAP=300s
APPROVAL_SERVE_HOOK_TIMEOUT=12s
APPROVAL_TG_WEBHOOK_SECRET=${{webhook_secret}}
APPROVAL_PUBLIC_URL={facade_url}
{p}TG_BOT_TOKEN=<your approval bot token, from @BotFather>
{p}TG_CHAT=<your Telegram user id>
EOF

{hermes_env}

# The Approved judge: the TENANT credential only. Never the agent credential.
cat > "$dir/judge.env" <<EOF
FACADE_URL={facade_url}
TENANT_TOKEN=${{tenant_token}}
TG_CHAT_ID=<your Telegram user id>
TG_BOT_TOKEN=<the judge's bot token>
WANDB_ENTITY=<your W&B entity>
WANDB_PROJECT=<your W&B project>
WANDB_API_KEY=<your W&B API key>
REVIEWER_MODEL=<a W&B Inference model id>
EOF
chmod 600 "$dir"/*.env
unset agent tenant_token webhook_secret
echo "credentials written to $dir/ (0600). Fill in the <placeholders> before importing."
"""


def build_bundle(
    *, mode: Mode, tenant: str, facade_url: str, approver: str = "operator"
) -> ConnectBundle:
    tenant, facade_url, approver = tenant.strip(), facade_url.strip().rstrip("/"), approver.strip()
    _validate(tenant, facade_url, approver)
    byo = mode == "byo"
    daemon_steps = f"""
# Create the daemon (public URL: Telegram and your harness reach it), then its environment.
maritime create {tenant}-daemon --repo {DAEMON_REPO} --public --port 8080
maritime env import {tenant}-daemon "$dir/daemon.env"
"""
    if byo:
        steps = daemon_steps
        hooks_yaml = """plugins:
  hook_callback_timeout: 600
hooks_auto_accept: true
hooks:
  pre_tool_call:
    - command: /usr/local/bin/hermes-hook-shim.sh
      timeout: 300
      fail_closed: true"""
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
        steps = (
            daemon_steps
            + f"""
# Create the gated Hermes machine (no public URL needed: the hook calls out), then its env.
maritime create {tenant}-hermes --repo {HERMES_REPO}
maritime env import {tenant}-hermes "$dir/hermes.env"
"""
        )
        hooks_yaml = None
        notes = [
            "The gated Hermes image writes its own hooks and consent, and refuses to start "
            "unless its self-check passes.",
        ]
        title = "Approved agent on Maritime"
    notes += [
        "Set the environment before the first boot, or restart each machine once after the "
        "import: env set after boot does not reach running processes.",
        "Attest the tenant's APPROVAL.md as the human approver before the gate will decide "
        "anything (approval policy attest --as human:<you>). The console never does this.",
        "Run the judge with judge.env: python -m approved serve (the console) or worker.",
    ]
    script = _credentials_block(tenant, facade_url, approver, byo) + steps
    return ConnectBundle(
        mode=mode, tenant=tenant, title=title, script=script, hooks_yaml=hooks_yaml, notes=notes
    )
