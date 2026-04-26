#!/usr/bin/env python3
"""
app.py — FastAPI backend for the Jobs AI web frontend
───────────────────────────────────────────────────────
Serves frontend/index.html and exposes REST + SSE endpoints
that drive the 7-phase pipeline.

Launch:
  python app.py
  or: uvicorn app:app --reload --port 8000
"""

import io
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Force UTF-8 stdout/stderr so Rich emoji don't crash on Windows cp1252
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from pipeline.config import OUTPUT_DIR, RESOURCES_DIR
from pipeline.phases import (
    phase1_ingest_resume,
    phase2_discover_jobs,
    phase3_score_jobs,
    phase4_tailor_resume,
    phase5_simulate_submission,
    phase6_update_tracker,
    phase7_run_report,
)
from pipeline.resume import _build_demo_resume, _read_resume, _save_tailored_resume

app = FastAPI(title="Jobs AI")

# ── Serve frontend ─────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse("frontend/index.html")

@app.get("/output/{path:path}")
def serve_output_file(path: str):
    p = OUTPUT_DIR / path
    if not p.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(p)

# ── Single-user in-memory state ───────────────────────────────────────────────

_S: dict = {
    "mode": "demo",
    "api_key": "",
    "threshold": 75,
    "job_titles": "Engineer",
    "location": "United States",
    "resume_text": None,
    "latex_source": None,
    "resume_filename": None,
    "profile": None,
    "jobs": None,
    "scored": None,
    "applications": None,
    "tracker_path": None,
    "done": set(),
    "error": {},
    "elapsed": {},
}

# ── Provider factory ──────────────────────────────────────────────────────────

def _make_provider():
    from pipeline.providers import DemoProvider, AnthropicProvider, OllamaProvider
    mode = _S.get("mode", "demo")
    if mode == "demo":
        return DemoProvider()
    if mode == "ollama":
        return OllamaProvider()
    key = _S.get("api_key", "")
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    return AnthropicProvider()

# ── SSE helper ────────────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"

def _run_phase_sse(phase: int, fn):
    """Generator: run *fn* in a thread, stream SSE heartbeats, emit done/error."""
    result_q: "queue.Queue[tuple]" = queue.Queue()

    def _worker():
        try:
            val = fn()
            result_q.put(("ok", val))
        except Exception as exc:
            result_q.put(("err", str(exc)))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t0 = time.time()

    yield _sse({"type": "start", "phase": phase})

    while t.is_alive():
        yield ": keep-alive\n\n"
        t.join(timeout=1.0)

    try:
        status, val = result_q.get_nowait()
    except queue.Empty:
        yield _sse({"type": "error", "phase": phase, "message": "phase timed out"})
        return

    elapsed = round(time.time() - t0, 1)

    if status == "err":
        _S["error"][phase] = val
        yield _sse({"type": "error", "phase": phase, "message": val})
    else:
        _S["done"].add(phase)
        _S["elapsed"][phase] = elapsed
        _S["error"].pop(phase, None)
        yield _sse({
            "type": "done",
            "phase": phase,
            "elapsed": elapsed,
            "data": _serialize(phase, val),
        })

def _serialize(phase: int, val) -> dict:
    """Convert a phase result value into a JSON-serialisable summary."""
    if phase == 1:
        p = val or {}
        return {
            "name": p.get("name", ""),
            "email": p.get("email", ""),
            "location": p.get("location", ""),
            "target_titles": p.get("target_titles", []),
            "top_hard_skills": p.get("top_hard_skills", []),
            "top_soft_skills": p.get("top_soft_skills", []),
            "resume_gaps": p.get("resume_gaps", []),
        }
    if phase == 2:
        jobs = val or []
        return {
            "total": len(jobs),
            "jobs": [
                {
                    "co": j.get("company", ""),
                    "role": j.get("title", ""),
                    "loc": j.get("location", ""),
                }
                for j in jobs[:10]
            ],
        }
    if phase == 3:
        scored = val or []
        thr = _S.get("threshold", 75)
        passed   = [j for j in scored if j.get("filter_status") == "passed"]
        auto     = [j for j in passed  if j.get("score", 0) >= thr]
        manual   = [j for j in passed  if j.get("score", 0) <  thr]
        below    = [j for j in scored  if j.get("filter_status") == "below_threshold"]
        filtered = [j for j in scored  if (j.get("filter_status") or "").startswith("filtered_")]
        return {
            "total": len(scored),
            "auto": len(auto),
            "manual": len(manual),
            "below": len(below),
            "filtered": len(filtered),
            "jobs": [
                {
                    "co":       j.get("company", ""),
                    "role":     j.get("title", ""),
                    "loc":      j.get("location", ""),
                    "score":    j.get("score", 0),
                    "skills":   ", ".join(list(j.get("skills_matched") or [])[:4]),
                    "status":   j.get("filter_status", ""),
                    "reasoning": j.get("reasoning", ""),
                }
                for j in sorted(passed, key=lambda x: x.get("score", 0), reverse=True)[:20]
            ],
        }
    if phase == 4:
        apps = val or []
        return {
            "count": len(apps),
            "titles": [
                f"{a.get('company','')} — {a.get('title','')}"
                for a in apps[:5]
            ],
        }
    if phase == 5:
        apps = val or []
        applied = sum(1 for a in apps if a.get("status") == "Applied")
        manual  = sum(1 for a in apps if a.get("status") == "Manual Required")
        return {"applied": applied, "manual": manual}
    if phase == 6:
        return {"tracker": Path(str(val)).name if val else ""}
    if phase == 7:
        return {}
    return {}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _guess_phase(name: str) -> int:
    if any(x in name for x in ("_Resume_", ".tex", ".pdf")):
        return 4
    if "Tracker" in name or ".xlsx" in name:
        return 6
    if "report" in name.lower() or name.endswith(".md"):
        return 7
    return 0

# ── Resume endpoints ──────────────────────────────────────────────────────────

@app.post("/api/resume/upload")
async def upload_resume(file: UploadFile = File(...)):
    import tempfile
    suffix = Path(file.filename or "resume.pdf").suffix or ".pdf"
    content = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        text, latex = _read_resume(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if not text:
        raise HTTPException(400, "Could not extract text from resume")
    _S["resume_text"] = text
    _S["latex_source"] = latex
    _S["resume_filename"] = file.filename
    return {"ok": True, "filename": file.filename, "length": len(text)}

@app.post("/api/resume/demo")
def load_demo_resume():
    _S["resume_text"] = _build_demo_resume()
    _S["latex_source"] = None
    _S["resume_filename"] = "demo_resume.txt"
    return {"ok": True, "filename": "demo_resume.txt"}

# ── Config ────────────────────────────────────────────────────────────────────

@app.post("/api/config")
async def update_config(req: Request):
    body = await req.json()
    for k in ("mode", "api_key", "threshold", "job_titles", "location"):
        if k in body:
            _S[k] = body[k]
    return {"ok": True}

# ── State ─────────────────────────────────────────────────────────────────────

@app.get("/api/state")
def get_state():
    profile = _S.get("profile") or {}
    scored  = _S.get("scored") or []
    thr     = _S.get("threshold", 75)
    passed   = [j for j in scored if j.get("filter_status") == "passed"]
    auto     = [j for j in passed  if j.get("score", 0) >= thr]
    manual   = [j for j in passed  if j.get("score", 0) <  thr]

    files = []
    if OUTPUT_DIR.exists():
        for f in sorted(OUTPUT_DIR.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and not f.name.startswith("."):
                files.append({
                    "name": f.name,
                    "phase": _guess_phase(f.name),
                    "size_kb": round(f.stat().st_size / 1024, 1),
                })

    return {
        "done": list(_S["done"]),
        "error": _S.get("error", {}),
        "elapsed": _S.get("elapsed", {}),
        "has_resume": bool(_S.get("resume_text")),
        "resume_filename": _S.get("resume_filename"),
        "mode": _S.get("mode", "demo"),
        "threshold": _S.get("threshold", 75),
        "job_titles": _S.get("job_titles", ""),
        "location": _S.get("location", "United States"),
        "profile": {
            "name": profile.get("name", ""),
            "email": profile.get("email", ""),
            "location": profile.get("location", ""),
            "target_titles": profile.get("target_titles", []),
            "top_hard_skills": profile.get("top_hard_skills", []),
            "top_soft_skills": profile.get("top_soft_skills", []),
            "resume_gaps": profile.get("resume_gaps", []),
        } if profile else None,
        "job_count": len(_S.get("jobs") or []),
        "scored_summary": {
            "total": len(scored),
            "auto": len(auto),
            "manual": len(manual),
            "below": sum(1 for j in scored if j.get("filter_status") == "below_threshold"),
            "filtered": sum(1 for j in scored if (j.get("filter_status") or "").startswith("filtered_")),
            "jobs": [
                {
                    "co":       j.get("company", ""),
                    "role":     j.get("title", ""),
                    "loc":      j.get("location", ""),
                    "score":    j.get("score", 0),
                    "skills":   ", ".join(list(j.get("skills_matched") or [])[:4]),
                    "status":   j.get("filter_status", ""),
                }
                for j in sorted(passed, key=lambda x: x.get("score", 0), reverse=True)[:20]
            ],
        } if scored else None,
        "applications": [
            {
                "co":           a.get("company", ""),
                "role":         a.get("title", ""),
                "loc":          a.get("location", ""),
                "score":        a.get("score", 0),
                "app_status":   a.get("status", ""),
                "confirmation": a.get("confirmation", ""),
                "date":         datetime.now().strftime("%Y-%m-%d"),
            }
            for a in (_S.get("applications") or [])
        ],
        "output_files": files,
    }

@app.post("/api/reset")
def reset_state():
    for k in ("profile", "jobs", "scored", "applications", "tracker_path"):
        _S[k] = None
    _S["done"] = set()
    _S["error"] = {}
    _S["elapsed"] = {}
    return {"ok": True}

# ── Phase 1 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/1/run")
def run_phase1():
    if not _S.get("resume_text"):
        raise HTTPException(400, "Load a resume first (sidebar → Resume)")
    preferred = [
        t.strip()
        for t in (_S.get("job_titles") or "").split(",")
        if t.strip() and t.strip().lower() != "engineer"
    ]

    def _fn():
        prov = _make_provider()
        result = phase1_ingest_resume(
            _S["resume_text"], prov,
            preferred_titles=preferred or None,
        )
        _S["profile"] = result
        return result

    return StreamingResponse(
        _run_phase_sse(1, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Phase 2 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/2/run")
def run_phase2():
    if not _S.get("profile"):
        raise HTTPException(400, "Run Phase 1 first")
    titles = [
        t.strip()
        for t in (_S.get("job_titles") or "Engineer").split(",")
        if t.strip()
    ]
    loc = _S.get("location") or "United States"

    def _fn():
        prov = _make_provider()
        result = phase2_discover_jobs(_S["profile"], titles, loc, prov)
        _S["jobs"] = result
        return result

    return StreamingResponse(
        _run_phase_sse(2, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Phase 3 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/3/run")
def run_phase3():
    if not _S.get("jobs"):
        raise HTTPException(400, "Run Phase 2 first")

    def _fn():
        prov = _make_provider()
        result = phase3_score_jobs(_S["jobs"], _S["profile"], prov, min_score=60)
        _S["scored"] = result
        return result

    return StreamingResponse(
        _run_phase_sse(3, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Phase 4 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/4/run")
def run_phase4():
    if not _S.get("scored"):
        raise HTTPException(400, "Run Phase 3 first")

    def _fn():
        prov   = _make_provider()
        scored = _S["scored"] or []
        passed = [j for j in scored if j.get("filter_status") == "passed"]
        apps   = []
        for job in passed:
            try:
                tailored = phase4_tailor_resume(
                    job, _S["profile"],
                    _S.get("resume_text", ""), prov,
                )
                _save_tailored_resume(
                    job, tailored, _S["profile"],
                    _S.get("latex_source"),
                    resume_text=_S.get("resume_text", ""),
                )
                apps.append({**job, "tailored": {}, "status": "Tailored"})
            except Exception as exc:
                apps.append({**job, "status": "Error", "notes": str(exc)})
        _S["applications"] = apps
        return apps

    return StreamingResponse(
        _run_phase_sse(4, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Phase 5 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/5/run")
def run_phase5():
    if not _S.get("applications"):
        raise HTTPException(400, "Run Phase 4 first")

    def _fn():
        apps      = _S["applications"] or []
        thr       = _S.get("threshold", 75)
        already   = set()
        results   = []
        auto_apps = [a for a in apps if a.get("score", 0) >= thr]
        for job in auto_apps:
            res = phase5_simulate_submission(job, already_applied=already)
            already.add((
                job.get("company", "").lower(),
                job.get("title", "").lower(),
            ))
            results.append({**job, **res})
        for job in (a for a in apps if a.get("score", 0) < thr):
            results.append({**job, "status": "Manual Required", "confirmation": "N/A"})
        _S["applications"] = results
        return results

    return StreamingResponse(
        _run_phase_sse(5, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Phase 6 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/6/run")
def run_phase6():
    if 5 not in _S["done"]:
        raise HTTPException(400, "Run Phase 5 first")

    def _fn():
        apps          = _S["applications"] or []
        tracker_path  = phase6_update_tracker(apps)
        _S["tracker_path"] = tracker_path
        return tracker_path

    return StreamingResponse(
        _run_phase_sse(6, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Phase 7 ───────────────────────────────────────────────────────────────────

@app.get("/api/phase/7/run")
def run_phase7():
    if 6 not in _S["done"]:
        raise HTTPException(400, "Run Phase 6 first")

    def _fn():
        prov   = _make_provider()
        apps   = _S["applications"] or []
        report = phase7_run_report(apps, _S.get("tracker_path"), prov)
        return report

    return StreamingResponse(
        _run_phase_sse(7, _fn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Jobs AI — starting on http://localhost:8000")
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
