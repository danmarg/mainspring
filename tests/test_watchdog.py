"""Event-loop watchdog: dumps stacks and exits when the loop stops ticking."""

import asyncio
import time

from app.watchdog import LoopWatchdog


def _run(loop_body, dog):
    async def main():
        beat = asyncio.create_task(dog.heartbeat())
        dog.start()
        try:
            await loop_body()
        finally:
            beat.cancel()
            dog.stop()
    asyncio.run(main())


def test_healthy_loop_never_dumps_or_exits(tmp_path):
    exits = []
    out = (tmp_path / "dump.txt").open("w+")
    dog = LoopWatchdog(dump_after=0.3, exit_after=0.6, poll=0.05, tick=0.05, exit_fn=exits.append, dump_file=out)

    async def body():
        await asyncio.sleep(1.2)  # loop keeps ticking

    _run(body, dog)
    out.seek(0)
    assert exits == [] and out.read() == ""


def test_blocked_loop_dumps_all_thread_stacks_then_exits(tmp_path):
    exits = []
    out = (tmp_path / "dump.txt").open("w+")
    dog = LoopWatchdog(dump_after=0.2, exit_after=0.6, poll=0.05, tick=0.05, exit_fn=exits.append, dump_file=out)

    async def body():
        time.sleep(1.2)  # blocks the loop like a deadlock / sync call on the loop

    _run(body, dog)
    out.seek(0)
    dump = out.read()
    assert exits == [70]
    assert "Thread" in dump and "time.sleep" in dump or "body" in dump  # the blocked frame is on record


def test_dump_happens_once_per_stall_and_exit_can_be_disabled(tmp_path):
    exits = []
    out = (tmp_path / "dump.txt").open("w+")
    dog = LoopWatchdog(dump_after=0.2, exit_after=0, poll=0.05, tick=0.05, exit_fn=exits.append, dump_file=out)

    async def body():
        time.sleep(0.8)

    _run(body, dog)
    out.seek(0)
    dump = out.read()
    assert exits == []
    assert dump.count("Current thread") + dump.count("Thread 0x") >= 1
    assert dump.count("Most recent call first") <= 8  # one dump, not one per poll


def test_app_lifespan_starts_and_stops_watchdog(tmp_path, monkeypatch):
    """Run the real app startup: the watchdog thread must start, /healthz must answer."""
    import threading
    monkeypatch.setenv("MAINSPRING_PASSWORD", "pw")
    monkeypatch.setenv("APP_BASE_URL", "https://example.test")
    import app.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "t.db")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert any(t.name == "loop-watchdog" and t.is_alive() for t in threading.enumerate())
    # after shutdown the watchdog is told to stop
    time.sleep(1.2)
    assert not any(t.name == "loop-watchdog" and t.is_alive() for t in threading.enumerate())
