"""``python -m approved.tryit``: the try-it container's one program (images/tryit/Dockerfile).

Order: bind the front first (so the platform sees a listener and ``/health`` answers 503 while
the rest boots), generate the throwaway credentials, run the demo's init step, start the fake
Telegram, the daemon and the judge, then supervise until SIGTERM. Exit codes: 0 stopped
cleanly, 2 configuration error (named on stderr, no value repeated).
"""

from __future__ import annotations

import signal
import subprocess
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
    Layout,
    TryitConfigError,
    load_settings,
)
from .front import App, FakeTelegramClient, make_server
from .runs import LiveBudget, Run, RunLimiter, RunManager, load_scenarios
from .supervisor import Reaper, Supervisor

EXIT_CONFIG = 2


def _missing_files() -> list[str]:
    needed = [
        NODE,
        JUDGE_PYTHON,
        AGENT_PYTHON,
        SH,
        APPROVAL_CLI,
        DAEMON_ENTRYPOINT,
        FAKE_TELEGRAM,
        AGENT_SCRIPT,
        INIT_SCRIPT,
        LOOPBACK_PRELOAD.removeprefix("file://"),
    ]
    return [p for p in needed if not Path(p).exists()]


def main() -> int:
    try:
        settings = load_settings()
    except TryitConfigError as exc:
        log("tryit.config.error", level="error", detail=str(exc))
        return EXIT_CONFIG
    missing = _missing_files()
    if missing:
        log("tryit.config.error", level="error", detail="image files missing", missing=missing)
        return EXIT_CONFIG

    layout = Layout()
    reaper = Reaper()
    sup = Supervisor(settings, layout, reaper)
    scenarios = load_scenarios(Path(AGENT_SCRIPT))
    calls_per_run = len(scenarios)
    budget = LiveBudget(
        layout.budget_file,
        daily_cap=settings.live_daily_cap,
        per_hour=settings.live_calls_per_hour,
    )

    def prepare(run: Run) -> None:
        """Pick this run's reviewer: live while the budget allows (charged up front), else
        offline with the reason shown on the page. Switching restarts the judge."""
        if not settings.live_ready:
            run.reviewer = "offline"
            return
        refusal = budget.reserve(calls_per_run)
        live = refusal is None
        if not sup.switch_judge(live):
            log("tryit.judge.slow", level="warning", mode="live" if live else "offline")
        run.reviewer = "live" if live else "offline"
        run.fallback = refusal
        log("tryit.run.reviewer", run=run.id, reviewer=run.reviewer, fallback=refusal)

    def spawn(run_id: str, scenario: str) -> subprocess.Popen[bytes]:
        return reaper.spawn(
            [AGENT_PYTHON, "-u", AGENT_SCRIPT],
            env=settings.agent_env(layout, run_id=run_id, scenario=scenario),
            cwd="/demo/agent",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    runs = RunManager(
        scenarios=scenarios,
        spawn=spawn,
        wait_exit=lambda proc: reaper.wait(proc) or 0,
        limiter=RunLimiter(settings.run_min_interval_s, settings.runs_per_hour),
        prepare=prepare,
        on_end=lambda _run: sup.verifier.request(),
    )

    def next_reviewer() -> tuple[str, str | None]:
        if not settings.live_ready:
            return "offline", None
        refusal = budget.check(calls_per_run)
        return ("live", None) if refusal is None else ("offline", refusal)

    app = App(
        settings=settings,
        judge_state_dir=layout.judge_state,
        runs=runs,
        chat=FakeTelegramClient(),
        budget=budget,
        health=sup.health.parts,
        judge_worker=sup.health.judge_worker,
        log_verify=sup.verifier.result,
        request_verify=sup.verifier.request,
        next_reviewer=next_reviewer,
        chat_generation=lambda: sup.fake_tg_generation,
        booted=lambda: sup.booted,
    )
    server = make_server(app, settings.port)
    stop = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log("tryit.signal", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    threading.Thread(target=server.serve_forever, name="front", daemon=True).start()
    threading.Thread(target=sup.health.run, args=(stop,), name="health", daemon=True).start()
    threading.Thread(target=sup.verifier.run, args=(stop,), name="verify", daemon=True).start()

    live_at_boot = next_reviewer()[0] == "live"
    log(
        "tryit.start",
        port=settings.port,
        reviewer="live" if live_at_boot else "offline",
        live_requested=settings.live_requested,
        live_not_ready=settings.live_not_ready_reason,
        window_s=settings.window_s,
        run_min_interval_s=settings.run_min_interval_s,
        runs_per_hour=settings.runs_per_hour,
        live_daily_cap=settings.live_daily_cap,
        frame_ancestors=list(settings.frame_ancestors),
    )
    sup.boot(stop, judge_live=live_at_boot)
    while not stop.wait(0.25):
        sup.watch()

    runs.stop()
    sup.shutdown()
    server.shutdown()
    log("tryit.stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
