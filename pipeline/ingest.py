"""
pipeline/ingest.py
──────────────────
Background ingestion worker.

* On boot, ``start_scheduler(...)`` launches an APScheduler
  ``BackgroundScheduler`` and dispatches a one-shot parallel backfill
  so the very first ``/api/jobs/feed`` request sees rows.

* Each registered :class:`pipeline.sources.JobSource` is then scheduled
  on its own ``cadence_seconds`` interval. Per-source locks make
  reruns mutually exclusive.

* Each run upserts into ``job_postings`` via :mod:`pipeline.job_repo`,
  records a row in ``source_runs``, and then calls
  ``mark_missing(...)`` so rows that vanished from the upstream feed
  for 3 consecutive runs flip ``deleted=1``.

The scheduler is best-effort: any source failure is logged and
isolated; one broken provider can't take down the rest.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed, wait as _futures_wait  # noqa: F401  (as_completed kept for _backfill_parallel)
from datetime import datetime, timezone
from typing import Callable

from . import job_repo
from .sources import registry as source_registry, JobSource
from .sources.base import infer_metadata


# ── Globals (module-level so reload-safe) ─────────────────────────────────────

_scheduler = None                              # apscheduler.schedulers.background.BackgroundScheduler
_locks: dict[str, threading.Lock] = {}         # one Lock per source name (drops overlapping ticks of the SAME source)
_connect: Callable[[], sqlite3.Connection] | None = None
_started = False

# Process-wide DB write serializer. SQLite (even in WAL mode) allows only ONE
# writer at a time; concurrent ingest workers were tripping over each other on
# the Pi and surfacing as "sqlite3.OperationalError: database is locked" even
# though `_connect` already passes a 30 s busy timeout — under 8-way parallel
# upserts the 30 s budget gets eaten by queued writers. Holding this lock only
# around the actual `with _conn() as conn:` write block keeps `source.fetch()`
# (network-bound, the actual reason we want parallelism) outside contention.
_WRITE_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    if _connect is None:
        raise RuntimeError("ingest.start_scheduler() was not called")
    return _connect()


# ── Single-source run ─────────────────────────────────────────────────────────

def run_one(source: JobSource) -> dict:
    """Execute one fetch+upsert+sweep cycle for *source*. Always logs to
    ``source_runs``. Never raises — caller can ignore failures.
    """
    name = source.name
    lock = _locks.setdefault(name, threading.Lock())
    if not lock.acquire(blocking=False):
        # Another tick is in flight — drop this one.
        return {"source": name, "skipped": True}

    started = _utc_now()
    fetched = inserted = 0
    err: str | None = None
    ok = False
    try:
        rows: list[dict] = []
        for raw in source.fetch(since=None):
            if not isinstance(raw, dict):
                continue
            rows.append(infer_metadata(raw))
            fetched += 1
        # Serialize the actual SQLite write — see the _WRITE_LOCK comment above.
        # `source.fetch()` ran outside the lock so 8 concurrent network fetches
        # still happen in parallel; only the upsert + mark_missing transaction
        # is single-writer.
        with _WRITE_LOCK:
            with _conn() as conn:
                inserted, _skipped = job_repo.upsert_many(conn, rows)
                # Soft-delete rows we didn't see in this run.
                job_repo.mark_missing(conn, name, started)
        ok = True
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        finished = _utc_now()
        try:
            # Telemetry write is inside the same lock — losing the
            # `source_runs` row on the very runs that hit lock errors would
            # erase the diagnostic signal we want most.
            with _WRITE_LOCK:
                with _conn() as conn:
                    job_repo.record_source_run(
                        conn, source=name, started_at=started,
                        finished_at=finished, ok=ok,
                        fetched=fetched, inserted=inserted, updated=0,
                        error=err,
                    )
        except Exception:
            traceback.print_exc()
        lock.release()
    return {
        "source": name, "ok": ok, "fetched": fetched, "inserted": inserted,
        "started": started, "finished": _utc_now(), "error": err,
    }


def force_run(source_name: str | None = None,
              wall_clock_timeout: float = 60.0) -> list[dict]:
    """Manual trigger used by ``POST /api/jobs/source-status`` and
    ``phase2_discover_jobs(force_live=True)``. With no name, runs every
    registered source in parallel and returns a per-source summary.

    The wall-clock cap matters: ``with ThreadPoolExecutor`` calls
    ``shutdown(wait=True)`` on exit, so without it a single hung HTTP
    fetch on a slow third-party API would pin the executor — and the
    FastAPI request thread — for minutes. We instead ``wait`` with a
    budget, tag unfinished futures as timed-out in the result, and
    ``shutdown(wait=False, cancel_futures=True)`` so queued work is
    cancelled and running work doesn't block return.
    """
    sources = [s for s in source_registry()
               if source_name is None or s.name == source_name]
    if not sources:
        return []
    out: list[dict] = []
    ex = ThreadPoolExecutor(max_workers=min(8, len(sources)))
    try:
        futs = {ex.submit(run_one, s): s.name for s in sources}
        done, not_done = _futures_wait(futs, timeout=wall_clock_timeout)
        for fut in done:
            try:
                out.append(fut.result())
            except Exception as exc:
                out.append({"source": futs[fut], "ok": False, "error": str(exc)})
        for fut in not_done:
            out.append({
                "source": futs[fut], "ok": False,
                "error": f"timeout after {wall_clock_timeout:.0f}s — source still running in background",
            })
    finally:
        # cancel_futures=True (Python 3.9+) cancels QUEUED-but-not-started work.
        # Running futures continue but we no longer block on them.
        ex.shutdown(wait=False, cancel_futures=True)
    return out


# ── Boot / scheduler ──────────────────────────────────────────────────────────

def _backfill_parallel(timeout_seconds: int = 60) -> None:
    """Fire every registered source once, in parallel, with a hard wall clock.
    Whatever returns within the budget is in the DB before users hit the SPA.
    """
    sources = source_registry()
    if not sources:
        return
    print(f"[ingest] backfilling {len(sources)} sources (timeout={timeout_seconds}s)…")
    started_at = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(run_one, s): s.name for s in sources}
        try:
            for fut in as_completed(futs, timeout=timeout_seconds):
                try:
                    res = fut.result(timeout=2)
                except Exception as exc:
                    res = {"source": futs[fut], "ok": False, "error": str(exc)}
                if res.get("ok"):
                    print(f"  ✓ {res['source']}: {res['fetched']} fetched, {res['inserted']} upserted")
                else:
                    print(f"  ✗ {res.get('source')}: {res.get('error')!s}")
        except Exception:
            pass
    elapsed = time.time() - started_at
    print(f"[ingest] backfill done in {elapsed:.1f}s")


def start_scheduler(connect: Callable[[], sqlite3.Connection],
                     *, run_backfill: bool = True,
                     backfill_timeout: int = 60) -> None:
    """Idempotent. Wires the SQLite connection factory, runs the first-boot
    backfill in a daemon thread (so FastAPI startup doesn't block), then
    starts the APScheduler background loop.
    """
    global _scheduler, _connect, _started
    _connect = connect
    if _started:
        return
    sources = source_registry()
    if not sources:
        print("[ingest] no sources registered; skipping scheduler")
        _started = True
        return

    if run_backfill:
        threading.Thread(
            target=_backfill_parallel,
            args=(backfill_timeout,),
            daemon=True,
            name="jobs-backfill",
        ).start()

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        print("[ingest] apscheduler not installed; running with backfill only")
        _started = True
        return

    sched = BackgroundScheduler(daemon=True, timezone="UTC")
    for src in sources:
        cadence = max(60, int(getattr(src, "cadence_seconds", 30 * 60)))
        sched.add_job(
            run_one, args=[src],
            trigger=IntervalTrigger(seconds=cadence,
                                    start_date=datetime.now(timezone.utc)),
            id=f"src:{src.name}",
            name=src.name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=cadence,
        )
    sched.start()
    _scheduler = sched
    _started = True
    print(f"[ingest] scheduler started for {len(sources)} sources")


def shutdown() -> None:
    global _scheduler, _started
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
    _started = False
