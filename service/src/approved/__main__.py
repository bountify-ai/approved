"""Command line.

``python -m approved worker [--once]``: run the advisory judge against one tenant facade.
``python -m approved evaluate [--offline] [--include-state DIR] [--limit N]``: score the judge
against the seeded scenarios (and optionally recorded decisions); prints a JSON report.

Worker exit codes: 0 stopped cleanly (SIGTERM/SIGINT, or ``--once`` caught up), 1 ``--once`` could
not complete a verified pass, 2 configuration error, 3 chain-break (persisted; the worker
refuses to follow until an operator investigates), 4 the state file cannot be trusted.
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from .config import ConfigError, InferenceSettings, Settings, load_inference_settings, load_settings
from .evaluate import load_dataset, run_offline, run_weave, scenarios_from_state
from .feedback import FeedbackSender, WeaveFeedbackClient
from .follow import FacadeClient
from .judge import CircuitBreaker, Judge, init_weave
from .logs import log
from .notify import Notifier, NullNotifier, TelegramNotifier
from .reviewer import LiveReviewer, OfflineReviewer, Reviewer
from .state import StateError, StateStore
from .worker import Worker

EXIT_CONFIG = 2
EXIT_STATE = 4


def build_reviewer(settings: InferenceSettings) -> Reviewer:
    if settings.offline:
        return OfflineReviewer()
    # load_settings guarantees both in live mode.
    assert settings.reviewer_model is not None
    assert settings.inference_api_key is not None
    return LiveReviewer(
        model=settings.reviewer_model,
        base_url=settings.inference_base_url,
        api_key=settings.inference_api_key.get_secret_value(),
        timeout_s=settings.judge_timeout_s,
        wandb_project=settings.weave_project,
    )


def build_notifier(settings: Settings) -> Notifier:
    if settings.tg_bot_token is None or not settings.tg_chat_id:
        log("notify.off", reason="TG_BOT_TOKEN(_FILE) or TG_CHAT_ID unset")
        return NullNotifier()
    return TelegramNotifier(
        settings.tg_bot_token,
        settings.tg_chat_id,
        api_base=settings.tg_api_base,
        timeout_s=settings.http_timeout_s,
    )


def build_worker(settings: Settings) -> Worker:
    store = StateStore(settings.state_dir, settings.facade_url)
    weave_handle = init_weave(settings)
    feedback = FeedbackSender(
        store,
        WeaveFeedbackClient(weave_handle.client) if weave_handle else None,
        timeout_s=settings.feedback_timeout_s,
    )
    judge = Judge(
        build_reviewer(settings),
        timeout_s=settings.judge_timeout_s,
        breaker=CircuitBreaker(settings.breaker_threshold, settings.breaker_cooldown_s),
        tracer=weave_handle.tracer if weave_handle else None,
        trace_url=settings.trace_url,
    )
    facade = FacadeClient(
        settings.facade_url, settings.tenant_token, timeout_s=settings.http_timeout_s
    )
    return Worker(
        facade=facade,
        judge=judge,
        notifier=build_notifier(settings),
        store=store,
        follow_limit=settings.follow_limit,
        poll_interval_s=settings.poll_interval_s,
        max_age_s=settings.judge_max_age_s,
        policy_file=settings.policy_file,
        on_decision=feedback.on_decision,
        on_idle=feedback.drain,
    )


def evaluate(args: argparse.Namespace) -> int:
    try:
        settings = load_inference_settings(offline=True if args.offline else None)
    except ConfigError as exc:
        log("config.error", level="error", detail=str(exc))
        return EXIT_CONFIG
    dataset = load_dataset(args.dataset)
    scenarios = list(dataset.scenarios)
    if args.include_state is not None:
        scenarios += scenarios_from_state(args.include_state)
    if args.limit is not None:
        scenarios = scenarios[: args.limit]
    reviewer = build_reviewer(settings)
    handle = None if settings.offline else init_weave(settings)
    if handle is None:
        report = run_offline(
            scenarios,
            reviewer,
            dataset_version=dataset.version,
            timeout_s=settings.judge_timeout_s,
        )
    else:
        report = run_weave(
            scenarios,
            reviewer,
            dataset_version=dataset.version,
            timeout_s=settings.judge_timeout_s,
            reviewer_model=settings.reviewer_model,
            trace_url=settings.trace_url,
        )
    print(report.model_dump_json(indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="approved", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    worker_cmd = sub.add_parser("worker", help="follow the tenant log and judge new requests")
    worker_cmd.add_argument(
        "--once", action="store_true", help="exit once the log is caught up (demos, cron)"
    )
    eval_cmd = sub.add_parser("evaluate", help="score the judge against expected decisions")
    eval_cmd.add_argument(
        "--offline", action="store_true", help="rules-based reviewer, no network, no Weave"
    )
    eval_cmd.add_argument("--dataset", type=Path, default=None, help="scenario JSON file")
    eval_cmd.add_argument(
        "--include-state",
        type=Path,
        default=None,
        metavar="STATE_DIR",
        help="also score decisions the worker recorded in STATE_DIR",
    )
    eval_cmd.add_argument("--limit", type=int, default=None, help="cap the number of examples")
    args = parser.parse_args(argv)
    if args.command == "evaluate":
        return evaluate(args)

    try:
        settings = load_settings()
    except ConfigError as exc:
        log("config.error", level="error", detail=str(exc))
        return EXIT_CONFIG
    try:
        worker = build_worker(settings)
    except StateError as exc:
        log("state.error", level="error", detail=str(exc))
        return EXIT_STATE

    def _graceful(signum: int, _frame: FrameType | None) -> None:
        log("worker.signal", signal=signal.Signals(signum).name)
        worker.stop()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    log(
        "worker.config",
        mode="offline" if settings.offline else "live",
        reviewer_model=settings.reviewer_model,
        weave_project=settings.weave_project,
        telegram=settings.telegram_enabled,
        judge_timeout_s=settings.judge_timeout_s,
    )
    return worker.run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
