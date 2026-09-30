"""The private try-it backend: one authenticated front and two isolated runtimes."""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path
from types import FrameType

from ..logs import log
from .config import (
    AGENT_PYTHON,
    AGENT_SCRIPT,
    APPROVAL_CLI,
    DAEMON_ENTRYPOINT,
    FAKE_TELEGRAM,
    INIT_SCRIPT,
    JUDGE_PYTHON,
    LOOPBACK_PRELOAD,
    NODE,
    SH,
    TryitConfigError,
    load_settings,
)
from .front import make_server
from .sessions import PrivateSessions, gateway_secret


def main() -> int:
    try:
        settings = load_settings()
        secret = gateway_secret(dict(os.environ))
        if not settings.live_ready:
            raise ValueError(settings.live_not_ready_reason or "TRYIT_LIVE=1 is required")
    except (TryitConfigError, ValueError, OSError) as exc:
        log("tryit.config.error", level="error", detail=str(exc))
        return 2
    needed = [
        NODE,
        SH,
        APPROVAL_CLI,
        DAEMON_ENTRYPOINT,
        FAKE_TELEGRAM,
        INIT_SCRIPT,
        JUDGE_PYTHON,
        AGENT_PYTHON,
        AGENT_SCRIPT,
        LOOPBACK_PRELOAD.removeprefix("file://"),
    ]
    missing = [name for name in needed if not Path(name).exists()]
    if missing:
        log("tryit.config.error", level="error", detail="image files missing", missing=missing)
        return 2

    app = PrivateSessions(settings, Path("/data"), secret)
    server = make_server(app, settings.port)
    stop = threading.Event()

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        log("tryit.signal", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    threading.Thread(target=server.serve_forever, name="front", daemon=True).start()
    app.run_sweeper(stop)
    app.shutdown()
    server.shutdown()
    log("tryit.stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
