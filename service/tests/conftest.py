from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

# urllib3 (pulled in lazily by weave) probes IPv6 support by creating a local socket when it
# is first imported. Import it at collection time, before pytest-socket disables sockets, so
# that host probe is not reported as a network attempt by a test.
import urllib3  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pydantic import SecretStr

from approved import logs
from approved.follow import FacadeClient
from approved.judge import CircuitBreaker, Judge, Tracer
from approved.notify import TelegramNotifier
from approved.reviewer import OfflineReviewer, Reviewer
from approved.state import StateStore
from approved.worker import Worker

from .fakes import FACADE_URL, TENANT_TOKEN, FakeFacade, FakeTelegram

#: 2026-09-29T12:00:30Z, thirty seconds after the synthetic requests' timestamp.
NOW = 1_790_683_230.0


class LogCapture:
    def __init__(self) -> None:
        self.stream = io.StringIO()

    def records(self, event: str | None = None) -> list[dict[str, Any]]:
        lines = [json.loads(x) for x in self.stream.getvalue().splitlines() if x]
        return [x for x in lines if event is None or x["event"] == event]


@pytest.fixture(autouse=True)
def logs_captured() -> Iterator[LogCapture]:
    """Capture structured logs; every test also proves no credential reached them."""
    capture = LogCapture()
    logs.set_stream(capture.stream)
    logs.METRICS.reset()
    yield capture
    text = capture.stream.getvalue()
    assert TENANT_TOKEN not in text, "tenant credential leaked into logs"
    assert "bot-secret" not in text, "bot token leaked into logs"


@pytest.fixture
def facade() -> Iterator[FakeFacade]:
    fake = FakeFacade()
    yield fake
    assert fake.violations == [], f"judge broke the facade contract: {fake.violations}"


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def make_worker(
    facade: FakeFacade, telegram: FakeTelegram, tmp_path: Path
) -> Callable[..., Worker]:
    def _make(
        reviewer: Reviewer | None = None,
        *,
        timeout_s: float = 2.0,
        breaker: CircuitBreaker | None = None,
        tracer: Tracer | None = None,
        state_dir: Path | None = None,
        clock: Callable[[], float] = lambda: NOW,
        facade_override: FakeFacade | None = None,
        **worker_kwargs: Any,
    ) -> Worker:
        source = facade_override or facade
        store = StateStore(state_dir or tmp_path / "state", FACADE_URL)
        judge = Judge(
            reviewer or OfflineReviewer(),
            timeout_s=timeout_s,
            breaker=breaker or CircuitBreaker(3, 60.0),
            tracer=tracer,
            trace_url=lambda call_id: f"https://wandb.ai/e/p/r/call/{call_id}" if call_id else None,
        )
        return Worker(
            facade=FacadeClient(FACADE_URL, SecretStr(TENANT_TOKEN), client=source.client()),
            judge=judge,
            notifier=TelegramNotifier(
                SecretStr("123456:bot-secret"),
                "42",
                api_base="https://tg.test",
                client=telegram.client(),
            ),
            store=store,
            poll_interval_s=0.01,
            clock=clock,
            **worker_kwargs,
        )

    return _make
