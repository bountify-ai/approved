"""The root Dockerfile (Maritime's GitHub route) must build what service/Dockerfile builds."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _build_steps(path: Path, prefix: str) -> list[str]:
    steps = []
    for line in path.read_text().splitlines():
        if line.startswith(("FROM", "COPY", "RUN uv", "ENTRYPOINT")) or "UV_" in line:
            steps.append(line.replace(prefix, ""))
    return steps


def test_root_and_service_dockerfiles_build_the_same_package() -> None:
    root = _build_steps(ROOT / "Dockerfile", "service/")
    service = _build_steps(ROOT / "service" / "Dockerfile", "")
    assert root == service


def test_root_image_serves_the_console() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert 'CMD ["serve"]' in text
    assert "STATE_DIR=/data/judge" in text
