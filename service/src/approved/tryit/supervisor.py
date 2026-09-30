"""Process supervision for the try-it container.

One supervisor process starts, watches and restarts three long-lived children, each in its
own process group so a dead child's own children go with it:

* the fake Telegram (``demo/fake-telegram/server.mjs``), on loopback;
* the approval.md daemon: the daemon image's own supervisor (``images/daemon/entrypoint.mjs``)
  unchanged, which runs ``approval up``, ``approval serve`` and the Telegram webhook verb.
  Its listen is moved to loopback by ``images/tryit/loopback.mjs``;
* the judge (``python -m approved serve``) with its console disabled, on loopback.

The container may run this process as PID 1 (``docker run`` without ``--init``) or as the
child of Maritime's init, so it reaps every child it has, known or orphaned, in one place
(:class:`Reaper`) and never calls ``Popen.wait``.

Nothing here writes to the approval log. The demo's init step (``demo/init.sh``) runs before
the daemon on every start; it seeds and attests the demo policy through the runtime's own
verbs when the store is fresh and does nothing otherwise.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from ..logs import log
from .config import (
    APPROVAL_CLI,
    CHILD_PATH,
    DAEMON_ENTRYPOINT,
    DAEMON_PORT,
    FAKE_TELEGRAM,
    FAKE_TG_PORT,
    INIT_SCRIPT,
    JUDGE_PORT,
    JUDGE_PYTHON,
    LOOPBACK_PRELOAD,
    NODE,
    SH,
    Layout,
    Settings,
)
from .credentials import Credentials, generate

__all__ = ["Child", "HealthProbe", "LogVerifier", "Reaper", "Supervisor", "get_json"]

RESTART_MIN_S = 0.5
RESTART_MAX_S = 30.0
STOP_GRACE_S = 20.0
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_json(url: str, timeout_s: float = 1.5, limit: int = 256 * 1024) -> tuple[int, Any]:
    """GET a loopback URL: (status, parsed JSON or None). Never raises; 0 means no answer.
    No proxy from the environment is ever consulted."""
    try:
        with _NO_PROXY.open(url, timeout=timeout_s) as response:
            raw = response.read(limit)
            status = response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(limit), exc.code
    except Exception:  # noqa: BLE001 - a probe that fails is an answer: down
        return 0, None
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, None


class Reaper:
    """Spawns every child and reaps every exit, including orphans reparented to PID 1."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[int, subprocess.Popen[bytes]] = {}

    def spawn(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str],
        cwd: str | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> subprocess.Popen[bytes]:
        with self._lock:  # a child cannot be reaped before it is tracked
            proc = subprocess.Popen(  # noqa: S603 - fixed argv from config, absolute paths
                argv,
                env=dict(env),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                close_fds=True,
            )
            self._procs[proc.pid] = proc
        return proc

    def reap(self) -> None:
        with self._lock:
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    return
                if pid == 0:
                    return
                proc = self._procs.pop(pid, None)
                if proc is not None:
                    proc.returncode = os.waitstatus_to_exitcode(status)

    def wait(self, proc: subprocess.Popen[bytes], timeout_s: float | None = None) -> int | None:
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while proc.returncode is None:
            self.reap()
            if proc.returncode is not None:
                break
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
        return proc.returncode


def _signal_group(proc: subprocess.Popen[bytes] | None, sig: int) -> None:
    if proc is None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


class Child:
    """One supervised child, restarted with a doubling backoff when it exits unasked."""

    def __init__(
        self,
        name: str,
        argv: list[str],
        env: Callable[[], Mapping[str, str]],
        *,
        cwd: str,
        reaper: Reaper,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.reaper = reaper
        self.clock = clock
        self.lock = threading.RLock()
        self.proc: subprocess.Popen[bytes] | None = None
        self.wanted = False
        self.started_at = 0.0
        self.next_start = 0.0
        self.backoff = RESTART_MIN_S
        self.starts = 0

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def start(self) -> None:
        with self.lock:
            self.wanted = True
            self.proc = self.reaper.spawn(self.argv, env=self.env(), cwd=self.cwd)
            self.started_at = self.clock()
            self.starts += 1
            log("tryit.child.start", child=self.name, pid=self.proc.pid, starts=self.starts)

    def tick(self) -> bool:
        """Notice an exit and restart after the backoff. True when it (re)started now."""
        with self.lock:
            now = self.clock()
            if not self.wanted:
                return False
            if self.alive:
                if now - self.started_at > 60:
                    self.backoff = RESTART_MIN_S
                return False
            if self.proc is not None:
                code = self.proc.returncode
                log("tryit.child.exit", level="warning", child=self.name, code=code)
                _signal_group(self.proc, signal.SIGKILL)  # whatever it left behind
                self.proc = None
                self.next_start = now + self.backoff
                self.backoff = min(self.backoff * 2, RESTART_MAX_S)
                return False
            if now >= self.next_start:
                self.start()
                return True
            return False

    def stop(self, grace_s: float = STOP_GRACE_S) -> None:
        with self.lock:
            self.wanted = False
            proc = self.proc
            if proc is None:
                return
            if proc.returncode is None:
                _signal_group(proc, signal.SIGTERM)
                if self.reaper.wait(proc, grace_s) is None:
                    log("tryit.child.kill", level="warning", child=self.name)
            _signal_group(proc, signal.SIGKILL)
            self.reaper.wait(proc, 5)
            self.proc = None

    def restart(self) -> None:
        with self.lock:
            self.stop()
            self.start()


class HealthProbe:
    """Polls the children's health endpoints on loopback; ``/health`` answers from this."""

    def __init__(self, interval_s: float = 1.0, *, settings: Settings | None = None) -> None:
        self._interval = interval_s
        self._settings = settings
        self._lock = threading.Lock()
        self._parts: dict[str, bool] = {"daemon": False, "fake_telegram": False, "judge": False}
        self._judge: dict[str, Any] = {}
        self._checked_at = 0.0

    def check_once(self) -> None:
        tg_port = self._settings.fake_tg_port if self._settings else FAKE_TG_PORT
        daemon_port = self._settings.daemon_port if self._settings else DAEMON_PORT
        judge_port = self._settings.judge_port if self._settings else JUDGE_PORT
        tg_status, _ = get_json(f"http://127.0.0.1:{tg_port}/healthz")
        d_status, daemon = get_json(f"http://127.0.0.1:{daemon_port}/health")
        j_status, judge = get_json(f"http://127.0.0.1:{judge_port}/health")
        daemon_ok = (
            d_status == 200
            and isinstance(daemon, dict)
            and daemon.get("facade") == "listening"
            and daemon.get("webhook") == "running"
        )
        worker = judge.get("worker") if isinstance(judge, dict) else None
        with self._lock:
            self._parts = {
                "daemon": daemon_ok,
                "fake_telegram": tg_status == 200,
                "judge": j_status == 200,
            }
            self._judge = worker if isinstance(worker, dict) else {}
            self._checked_at = time.monotonic()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.check_once()
            stop.wait(self._interval)

    def parts(self) -> dict[str, bool]:
        with self._lock:
            if time.monotonic() - self._checked_at > 10:
                return dict.fromkeys(self._parts, False)
            return dict(self._parts)

    def judge_worker(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._judge)


class LogVerifier:
    """Runs core's ``approval log verify --json`` in the store, in the background, on demand
    and at most every ``min_gap_s``; the page shows the cached result. Read-only."""

    def __init__(self, layout: Layout, reaper: Reaper, min_gap_s: float = 15.0) -> None:
        self._layout = layout
        self._reaper = reaper
        self._min_gap = min_gap_s
        self._wanted = threading.Event()
        self._lock = threading.Lock()
        self._result: dict[str, Any] = {"status": "not-checked-yet"}
        self._last = 0.0

    def request(self) -> None:
        self._wanted.set()

    def result(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._result)

    def _verify(self) -> dict[str, Any]:
        env = {
            "PATH": CHILD_PATH,
            "HOME": "/tmp",  # noqa: S108
            "NODE_ENV": "production",
            "APPROVAL_CLI": APPROVAL_CLI,
            "APPROVAL_DATA_DIR": str(self._layout.data),
            "APPROVAL_STATE_DIR": str(self._layout.tryit / "runtime-state"),
        }
        proc = self._reaper.spawn(
            [NODE, APPROVAL_CLI, "log", "verify", "--json"],
            env=env,
            cwd=str(self._layout.store),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        timer = threading.Timer(20, _signal_group, args=(proc, signal.SIGKILL))
        timer.start()
        try:
            assert proc.stdout is not None
            raw = proc.stdout.read(64 * 1024)
        finally:
            timer.cancel()
            proc.stdout.close() if proc.stdout else None
        self._reaper.wait(proc, 5)
        try:
            body = json.loads(raw)
        except ValueError:
            return {"status": "unreadable", "checked_at": time.time()}
        head = body.get("head") if isinstance(body, dict) else None
        return {
            "status": str(body.get("status")) if isinstance(body, dict) else "unreadable",
            "records": body.get("records") if isinstance(body, dict) else None,
            "head_seq": head.get("seq") if isinstance(head, dict) else None,
            "checked_at": time.time(),
        }

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self._wanted.wait(60)
            if stop.is_set():
                return
            gap = time.monotonic() - self._last
            if gap < self._min_gap:
                stop.wait(self._min_gap - gap)
            self._wanted.clear()
            self._last = time.monotonic()
            try:
                result = self._verify()
            except Exception as exc:  # noqa: BLE001 - a failed check is shown, never fatal
                result = {"status": f"check-failed:{type(exc).__name__}", "checked_at": time.time()}
            with self._lock:
                self._result = result


class Supervisor:
    def __init__(self, settings: Settings, layout: Layout, reaper: Reaper) -> None:
        self.settings = settings
        self.layout = layout
        self.reaper = reaper
        self.health = HealthProbe(settings=settings)
        self.verifier = LogVerifier(layout, reaper)
        self.booted = False
        self._lifecycle_lock = threading.RLock()
        self._stopped = False
        self.boot_error: str | None = None
        self.judge_live = False
        self.fake_tg_generation = 0
        self._credentials: Credentials | None = None
        self.fake_tg = Child(
            "fake-telegram",
            [NODE, "--import", LOOPBACK_PRELOAD, FAKE_TELEGRAM],
            settings.fake_tg_env,
            cwd="/demo/fake-telegram",
            reaper=reaper,
        )
        self.daemon = Child(
            "daemon",
            [NODE, "--import", LOOPBACK_PRELOAD, DAEMON_ENTRYPOINT],
            self._daemon_env,
            cwd=str(layout.data),
            reaper=reaper,
        )
        self.judge = Child(
            "judge",
            [JUDGE_PYTHON, "-m", "approved", "serve"],
            lambda: settings.judge_env(layout, live=self.judge_live),
            cwd="/app",
            reaper=reaper,
        )

    def _daemon_env(self) -> dict[str, str]:
        assert self._credentials is not None
        return self.settings.daemon_env(self._credentials.daemon_env(), self.layout)

    @property
    def children(self) -> list[Child]:
        return [self.fake_tg, self.daemon, self.judge]

    def credential_values(self) -> tuple[str, ...]:
        return self._credentials.values() if self._credentials else ()

    # ------------------------------------------------------------ boot

    def _init_store(self, stop: threading.Event) -> bool:
        env = {
            "PATH": CHILD_PATH,
            "HOME": "/tmp",  # noqa: S108
            "NODE_ENV": "production",
            "APPROVAL_CLI": APPROVAL_CLI,
            "APPROVAL_DATA_DIR": str(self.layout.data),
            "APPROVAL_STATE_DIR": str(self.layout.tryit / "runtime-state"),
        }
        backoff = 2.0
        while not stop.is_set():
            proc = self.reaper.spawn([SH, INIT_SCRIPT], env=env, cwd=str(self.layout.data))
            code = self.reaper.wait(proc, 120)
            if code == 0:
                return True
            _signal_group(proc, signal.SIGKILL)
            self.boot_error = "init-failed"
            log("tryit.init.failed", level="error", code=code)
            stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)
        return False

    def _await(self, part: str, stop: threading.Event, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while not stop.is_set() and time.monotonic() < deadline:
            self.reaper.reap()
            self.health.check_once()
            if self.health.parts().get(part):
                return True
            stop.wait(0.25)
        return False

    def boot(self, stop: threading.Event, *, judge_live: bool) -> None:
        self.layout.data.mkdir(parents=True, exist_ok=True)
        self._credentials = generate(self.layout.secrets)
        log("tryit.credentials", written=str(self.layout.secrets), mode="0600")
        if not self._init_store(stop):
            return
        self.boot_error = None
        self.fake_tg_generation += 1
        self.fake_tg.start()
        if not self._await("fake_telegram", stop, 30):
            if not stop.is_set():
                self.boot_error = "fake-telegram-unavailable"
                log("tryit.boot.failed", level="error", part="fake_telegram")
            return
        if stop.is_set():
            self.shutdown()
            return
        self.daemon.start()
        if not self._await("daemon", stop, 90):
            if not stop.is_set():
                self.boot_error = "daemon-unavailable"
                log("tryit.boot.failed", level="error", part="daemon")
            return
        if stop.is_set():
            self.shutdown()
            return
        self.judge_live = judge_live
        self.judge.start()
        self._await("judge", stop, 60)
        if stop.is_set():
            self.shutdown()
            return
        self.booted = True
        self.verifier.request()
        log("tryit.boot.done", parts=self.health.parts(), judge="live" if judge_live else "offline")

    # ------------------------------------------------------------ steady state

    def watch(self) -> None:
        """One supervision pass: reap, then restart what died. The fake Telegram keeps the
        daemon's webhook registration in memory, so its restart restarts the daemon too."""
        with self._lifecycle_lock:
            self.reaper.reap()
            if not self.booted or self._stopped:
                return
            if self.fake_tg.tick():
                self.fake_tg_generation += 1
                log("tryit.child.cascade", child="daemon", reason="fake-telegram restarted")
                self.daemon.restart()
            self.daemon.tick()
            self.judge.tick()

    def switch_judge(self, live: bool, timeout_s: float = 45.0) -> bool:
        """Restart the judge with the live or the offline reviewer. Its state (cursor, ledger)
        is on disk, so it resumes where it was. True when it answers healthy in time."""
        with self._lifecycle_lock, self.judge.lock:
            if self._stopped:
                return False
            if self.judge_live == live and self.judge.alive:
                return True
            log("tryit.judge.switch", to="live" if live else "offline")
            self.judge.stop()
            self.judge_live = live
            self.judge.start()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.reaper.reap()
            self.health.check_once()
            if self.health.parts().get("judge"):
                return True
            time.sleep(0.25)
        return False

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            self._stopped = True
            self.booted = False
            for child in (self.judge, self.daemon, self.fake_tg):
                child.stop()
            self.reaper.reap()
