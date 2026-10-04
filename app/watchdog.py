"""
Event-loop watchdog.

A heartbeat task on the event loop stamps the time every second; a plain thread
watches the stamp. If the loop stops ticking (a deadlock or a long blocking call —
/healthz then times out and Fly routes 503s indefinitely) the watchdog:

  1. after DUMP_AFTER seconds: dumps every thread's stack and the open SQLite write
     transactions to stderr (→ Fly logs), once per stall, so the cause is on record;
  2. after EXIT_AFTER seconds: exits the process non-zero so Fly restarts the
     machine instead of leaving it wedged until someone notices.

WATCHDOG_EXIT_AFTER_S=0 disables the exit (the stack dump still happens).
"""

import asyncio
import faulthandler
import logging
import os
import sys
import threading
import time

log = logging.getLogger(__name__)


class LoopWatchdog:
    def __init__(
        self,
        dump_after: float = 15.0,
        exit_after: float = 90.0,
        poll: float = 1.0,
        tick: float = 1.0,
        exit_fn=os._exit,
        dump_file=None,
    ):
        self.dump_after = dump_after
        self.exit_after = exit_after
        self.poll = poll
        self.tick = tick
        self._exit = exit_fn
        self._dump_file = dump_file
        self._last_tick = time.monotonic()
        self._dumped = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    async def heartbeat(self) -> None:
        while True:
            self._last_tick = time.monotonic()
            await asyncio.sleep(self.tick)

    def start(self) -> None:
        self._last_tick = time.monotonic()
        self._thread = threading.Thread(target=self._watch, name="loop-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        while not self._stop.wait(self.poll):
            stalled = time.monotonic() - self._last_tick
            if stalled < self.dump_after:
                self._dumped = False  # recovered; re-arm for the next stall
                continue
            if not self._dumped:
                self._dumped = True
                self._report(stalled)
            if self.exit_after and stalled >= self.exit_after:
                log.critical("event loop stalled %.0fs — exiting so Fly restarts the machine", stalled)
                sys.stderr.flush()
                self._exit(70)
                return

    def _report(self, stalled: float) -> None:
        try:
            from app.db import open_write_transactions
            txns = open_write_transactions()
        except Exception:
            txns = ["<unavailable>"]
        log.error("event loop stalled %.0fs; open write transactions: %s; thread stacks follow", stalled, txns)
        faulthandler.dump_traceback(file=self._dump_file or sys.stderr, all_threads=True)


def start_watchdog() -> LoopWatchdog | None:
    """Create + start the watchdog; returns it so the caller can run its heartbeat."""
    exit_after = float(os.getenv("WATCHDOG_EXIT_AFTER_S", "90"))
    dog = LoopWatchdog(dump_after=float(os.getenv("WATCHDOG_DUMP_AFTER_S", "15")), exit_after=exit_after)
    dog.start()
    return dog
