"""Local credentials for one tenant: ``./.approved/<tenant>/``, directory 0700, files 0600.

Generated here with :func:`secrets.token_hex`, read back by name, and written into
``maritime env import`` files beside them. Never printed, never on argv, never logged: the
CLI prints paths only. Reruns reuse the files (idempotent provisioning keeps the pair a
running daemon already holds).
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from ..console.bundle import GENERATED_SECRETS, EnvVar, SecretRef

__all__ = ["CredentialDir", "CredentialError"]

_FILE = re.compile(r"^[a-z_]{3,40}$")


class CredentialError(RuntimeError):
    pass


def _write_0600(path: Path, data: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class CredentialDir:
    def __init__(self, root: Path, tenant: str) -> None:
        self.path = Path(root) / tenant

    @property
    def exists(self) -> bool:
        return all((self.path / name).is_file() for name in GENERATED_SECRETS)

    def ensure(self) -> None:
        """Create the directory (0700) and any missing generated secret (0600)."""
        self.path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path, 0o700)
        for name in GENERATED_SECRETS:
            target = self.path / name
            if not target.exists():
                _write_0600(target, secrets.token_hex(24))

    def store(self, name: SecretRef, value: str) -> None:
        if not _FILE.match(name):
            raise CredentialError("bad credential name")
        self.path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path, 0o700)
        _write_0600(self.path / name, value.strip())

    def get(self, name: SecretRef) -> str | None:
        target = self.path / name
        if not target.is_file():
            return None
        mode = target.stat().st_mode & 0o777
        if mode & 0o077:
            raise CredentialError(f"{target} is mode {mode:o}; credentials must be 0600")
        value = target.read_text(encoding="utf-8").strip()
        return value or None

    def values(self) -> list[str]:
        out = []
        for child in self.path.glob("*"):
            if child.is_file() and not child.name.endswith(".env") and _FILE.match(child.name):
                value = child.read_text(encoding="utf-8").strip()
                if value:
                    out.append(value)
        return out

    def env_file(self, name: str, variables: list[EnvVar]) -> Path | None:
        """Write the variables that ``maritime env import`` should carry; ``None`` if empty.

        Secret references are resolved from this directory; a reference with no stored value
        (an optional credential the operator did not supply) is left out.
        """
        lines = []
        for var in variables:
            if var.ref is not None:
                value = self.get(var.ref)
                if value is None:
                    continue
                lines.append(f"{var.name}={value}")
            elif not var.placeholder:
                lines.append(f"{var.name}={var.value}")
        if not lines:
            return None
        target = self.path / f"{name}.env"
        _write_0600(target, "\n".join(lines) + "\n")
        return target
