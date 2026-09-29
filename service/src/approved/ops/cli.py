"""The operator subcommands: ``provision``, ``connect``, ``ask``, ``status``.

Exit codes: 0 done; 2 usage or refused input (including a protected machine name);
3 blocked (``ask``: the gate is waiting for a human); 4 timed out (``ask``); 5 a Maritime
or provisioning failure.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import stat
from importlib import resources
from pathlib import Path

from ..console.bundle import BundleError, build_bundle, validate_tenant
from .ask import ask
from .credentials import CredentialDir, CredentialError
from .maritime import Maritime, MaritimeError, SecretGuard
from .names import ProtectedName, refuse_protected
from .provision import ProvisionError, ProvisionOptions, provision
from .status import status

__all__ = ["add_subcommands", "run"]

EXIT_OK, EXIT_USAGE, EXIT_BLOCKED, EXIT_TIMEOUT, EXIT_FAILED = 0, 2, 3, 4, 5
COMMANDS = ("provision", "connect", "ask", "status")


def add_subcommands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("provision", help="create a tenant's machines on Maritime")
    p.add_argument("tenant")
    p.add_argument("--policy", type=Path, required=True, help="the tenant's APPROVAL.md")
    p.add_argument("--approver", default="operator", help="human approver id (attest step)")
    p.add_argument("--tg-chat", default=None, help="approver's Telegram chat id (not secret)")
    p.add_argument(
        "--tg-bot-token-file", type=Path, default=None, help="file holding the bot token"
    )
    p.add_argument("--hermes", action="store_true", help="also create the gated Hermes")
    p.add_argument("--hermes-model", default=None)
    p.add_argument("--hermes-provider", default=None)
    p.add_argument("--hermes-base-url", default=None)
    p.add_argument("--hermes-key-env", default=None)
    p.add_argument("--judge", action="store_true", help="also create the Approved judge")
    p.add_argument(
        "--judge-bot-token-file",
        type=Path,
        default=None,
        help="the judge's OWN bot token (a second bot, never the approval bot); needed by --judge",
    )
    p.add_argument(
        "--replace-policy",
        action="store_true",
        help="overwrite a different policy on an existing daemon (only with no open request)",
    )
    p.add_argument("--credentials", type=Path, default=Path(".approved"))
    p.add_argument("--wait-s", type=float, default=300.0)

    c = sub.add_parser("connect", help="write a bring-your-own-Hermes bundle to a directory")
    c.add_argument("tenant")
    c.add_argument("--facade-url", required=True)
    c.add_argument("--approver", default="operator")
    c.add_argument("--out", type=Path, default=None, help="default: ./approved-connect-<tenant>")
    c.add_argument("--force", action="store_true", help="overwrite an existing bundle")

    a = sub.add_parser("ask", help="send one prompt to a gated Hermes (detached-safe)")
    a.add_argument("agent")
    a.add_argument("prompt", nargs="?")
    a.add_argument("--job", default=None, help="collect an earlier detached call")
    a.add_argument("--wait-s", type=float, default=280.0)
    a.add_argument(
        "--allow-target",
        default=None,
        metavar="EXACT_NAME",
        help="permit this one protected machine (exact, case-sensitive resolved name); read-only",
    )

    s = sub.add_parser("status", help="machine state, public health and log verify")
    s.add_argument("tenant")
    s.add_argument(
        "--allow-target",
        default=None,
        metavar="EXACT_NAME",
        help="the daemon machine's exact name, when it is a protected one (read-only)",
    )


def _guard(credentials_root: Path, tenant: str | None) -> SecretGuard:
    guard = SecretGuard()
    if tenant:
        with contextlib.suppress(OSError):
            guard.add(*CredentialDir(credentials_root, tenant).values())
    return guard


def _connect(args: argparse.Namespace) -> int:
    bundle = build_bundle(
        mode="byo", tenant=args.tenant, facade_url=args.facade_url, approver=args.approver
    )
    out: Path = args.out or Path(f"approved-connect-{bundle.tenant}")
    files = {
        "connect.sh": (bundle.script, 0o700),
        "hermes-config.yaml": ((bundle.hooks_yaml or "") + "\n", 0o644),
        "hermes-hook-shim.sh": (
            resources.files("approved.console")
            .joinpath("downloads", "hermes-hook-shim.sh")
            .read_text(encoding="utf-8"),
            0o755,
        ),
        "NOTES.txt": ("\n".join(f"- {n}" for n in bundle.notes) + "\n", 0o644),
    }
    if out.exists() and any((out / name).exists() for name in files) and not args.force:
        print(f"{out}/ already holds a bundle; pass --force to overwrite")
        return EXIT_USAGE
    out.mkdir(parents=True, exist_ok=True)
    for name, (text, mode) in files.items():
        target = out / name
        target.write_text(text, encoding="utf-8")
        os.chmod(target, mode)
    print(f"wrote {', '.join(files)} to {out}/")
    print(f"next: bash {out}/connect.sh  (generates credentials locally, 0600)")
    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    try:
        if args.command == "connect":
            refuse_protected(args.tenant)
            return _connect(args)
        if args.command == "provision":
            validate_tenant(args.tenant)
            maritime = Maritime(_guard(args.credentials, args.tenant))
            provision(
                maritime,
                ProvisionOptions(
                    tenant=args.tenant,
                    policy=args.policy,
                    approver=args.approver,
                    tg_chat=args.tg_chat,
                    tg_bot_token_file=args.tg_bot_token_file,
                    hermes=args.hermes or bool(args.hermes_model),
                    hermes_model=args.hermes_model,
                    hermes_provider=args.hermes_provider,
                    hermes_base_url=args.hermes_base_url,
                    hermes_key_env=args.hermes_key_env,
                    judge=args.judge,
                    judge_bot_token_file=args.judge_bot_token_file,
                    replace_policy=args.replace_policy,
                    credentials_root=args.credentials,
                    wait_s=args.wait_s,
                ),
            )
            return EXIT_OK
        if args.command == "ask":
            outcome = ask(
                Maritime(SecretGuard()),
                args.agent,
                args.prompt,
                job=args.job,
                wait_s=args.wait_s,
                allow_target=args.allow_target,
            )
            return {
                "allowed": EXIT_OK,
                "blocked": EXIT_BLOCKED,
                "timeout": EXIT_TIMEOUT,
            }.get(outcome.kind, EXIT_FAILED)
        if args.command == "status":
            status(
                Maritime(_guard(Path(".approved"), args.tenant)),
                args.tenant,
                allow_target=args.allow_target,
            )
            return EXIT_OK
    except (ProtectedName, BundleError, ValueError, CredentialError) as exc:
        print(f"refused: {exc}")
        return EXIT_USAGE
    except (MaritimeError, ProvisionError, OSError) as exc:
        print(f"failed: {exc}")
        return EXIT_FAILED
    raise SystemExit(f"unknown command {args.command}")


def is_secret_file(path: Path) -> bool:
    return stat.S_IMODE(path.stat().st_mode) == 0o600
