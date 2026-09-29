"""FastAPI dashboard — web UI for reviewing and managing job matches.

Serves HTMX-powered HTML pages + JSON API endpoints.
Run with: job-agent dashboard
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import secrets
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter, BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, Query, Request,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import load_settings, AppSettings
from app.database import ApplyQueueError, Database, DecisionRecord
from app.logging_config import setup_logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directory paths
# ---------------------------------------------------------------------------

DASHBOARD_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"

# ---------------------------------------------------------------------------
# App state — shared across requests
# ---------------------------------------------------------------------------

_state: dict[str, Any] = {}


def get_db() -> Database:
    return _state["db"]


def get_settings() -> AppSettings:
    return _state["settings"]


# ---------------------------------------------------------------------------
# Background task tracking
# ---------------------------------------------------------------------------

_running_tasks: dict[str, bool] = {
    "scrape": False,
    "evaluate": False,
}


# ---------------------------------------------------------------------------
# Lifespan — open/close DB
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open database on startup, close on shutdown."""
    settings = load_settings()
    setup_logging(settings.db_path.parent.parent / "logs", settings.log_level)

    db = Database(settings.db_path)
    await db.initialize()

    _state["db"] = db
    _state["settings"] = settings

    logger.info("Dashboard started — DB at %s", settings.db_path)
    yield

    await db.close()
    logger.info("Dashboard stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Job Agent Dashboard",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def _score_color(score: float | None) -> str:
    if score is None:
        return "neutral"
    if score >= 0.7:
        return "green"
    if score >= 0.4:
        return "yellow"
    return "red"


def _format_salary(sal_min: float | None, sal_max: float | None) -> str:
    if sal_min and sal_max:
        return f"${sal_min / 1000:.0f}k – ${sal_max / 1000:.0f}k"
    if sal_min:
        return f"From ${sal_min / 1000:.0f}k"
    if sal_max:
        return f"Up to ${sal_max / 1000:.0f}k"
    return "Not listed"


def _humanize_lane(lane: str | None) -> str:
    """'frontend_developer' → 'Frontend Developer'."""
    if not lane:
        return "Unassigned"
    return lane.replace("_", " ").title()


def _enabled_lane_names() -> list[str]:
    return [lane.name for lane in get_settings().profile.enabled_lanes]


def _doc_lanes(job: dict) -> list[str]:
    """Lanes a resume can be generated for: every enabled lane for 'both', else the job's lane."""
    lanes = _enabled_lane_names()
    if job.get("search_lane") == "both":
        return lanes
    if job.get("search_lane") in lanes:
        return [job["search_lane"]]
    return lanes[:1]


def _cover_letter_panel_ctx(job_id: int, lane: str, result: Any, *,
                            saved: bool = False) -> dict[str, Any]:
    """Template context for partials/cover_letter.html from a CoverLetterResult."""
    gen = result.gen_result
    return {
        "job_id": job_id, "lane": lane, "text": result.text,
        "from_cache": result.from_cache, "edited": result.edited,
        "model": result.model_used, "cost": gen.cost_display if gen else None,
        "context_source": result.context_source, "saved": saved,
    }


async def _download_filename(db: Database, job_id: int, kind: str, fmt: str) -> str:
    """"{last_name}_{company_slug}_{kind}.{fmt}" for download responses."""
    from app.resume_generator.linkedin_parser import load_linkedin_data

    job = await db.get_job(job_id)
    company_slug = re.sub(r"[^A-Za-z0-9]+", "_", (job.company if job else "") or "").strip("_")
    try:
        last_name = load_linkedin_data().last_name or kind
    except FileNotFoundError:
        last_name = kind
    return f"{last_name}_{company_slug or 'job'}_{kind}.{fmt}"


def _parse_force(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "on") if value is not None else False


async def _lane_and_force(request: Request, lane: str | None, force: Any) -> tuple[str | None, bool]:
    """Read lane/force from form fields, falling back to a JSON body (same as api_decide)."""
    if lane is not None:
        return lane, _parse_force(force)
    try:
        body = await request.json()
    except Exception:
        return None, False
    return body.get("lane"), _parse_force(body.get("force", False))


def _resume_panel_ctx(job_id: int, lane: str, resume: Any, *, from_cache: bool,
                      cost: str | None, model: str | None,
                      generated_at: str | None = None) -> dict[str, Any]:
    return {
        "job_id": job_id, "lane": lane, "resume": resume, "from_cache": from_cache,
        "cost": cost, "model": model, "generated_at": generated_at,
    }


# Register filters for Jinja2
templates.env.filters["score_color"] = _score_color
templates.env.filters["format_salary"] = lambda row: _format_salary(
    row.get("salary_min"), row.get("salary_max")
)
templates.env.globals["score_color"] = _score_color
templates.env.globals["format_salary"] = _format_salary
templates.env.filters["humanize_lane"] = _humanize_lane


# ---------------------------------------------------------------------------
# Page routes (HTML)
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Dashboard home — stats + top matches."""
    db = get_db()
    stats = await db.get_stats()
    top_matches, _ = await db.get_jobs_paginated(
        status="evaluated", limit=5, sort_by="eval_score", sort_dir="DESC"
    )
    recent_maybes, _ = await db.get_jobs_paginated(
        status="maybe", limit=5, sort_by="eval_score", sort_dir="DESC"
    )

    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "stats": stats,
        "top_matches": top_matches,
        "recent_maybes": recent_maybes,
        "running_tasks": _running_tasks,
        "lanes": _enabled_lane_names(),
    })


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(
    request: Request,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    sort: str = Query("date_scraped"),
    dir: str = Query("DESC"),
    lane: str | None = Query(None),
):
    """Job list page with status + lane filters."""
    db = get_db()
    offset = (page - 1) * limit
    jobs, total = await db.get_jobs_paginated(
        status=status, limit=limit, offset=offset, sort_by=sort, sort_dir=dir, lane=lane,
    )
    total_pages = max(1, (total + limit - 1) // limit)
    all_stats = await db.get_stats(lane=lane)

    ctx = {
        "request": request,
        "jobs": jobs,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "limit": limit,
        "current_status": status,
        "current_sort": sort,
        "current_dir": dir,
        "current_lane": lane,
        "lanes": _enabled_lane_names(),
        "stats": all_stats,
    }

    # HTMX pagination swaps just the table; filter tabs swap the whole view
    # (full page + hx-select) so both tab rows re-render with the new filters.
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "job-list":
        return templates.TemplateResponse(request, "partials/job_table.html", ctx)

    return templates.TemplateResponse(request, "jobs.html", ctx)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail_page(request: Request, job_id: int):
    """Job detail page."""
    db = get_db()
    job = await db.get_job_with_evaluation(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Jobs found by multiple lanes get one evaluation per lane, shown side by side
    lane_evals = []
    if job.get("search_lane") == "both":
        for lane_name in _enabled_lane_names():
            evaluation = await db.get_evaluation(job_id, lane=lane_name)
            lane_evals.append({"lane": lane_name, "eval": evaluation})

    # Previously generated documents render populated instead of the opt-in stubs
    from app.polisher.pipeline import DOC_TYPE as COVER_LETTER, read_context_source
    from app.resume_generator.pipeline import DOC_TYPE as RESUME, load_saved_resume

    doc_lanes = _doc_lanes(job)
    existing_resumes: dict[str, dict[str, Any]] = {}
    existing_cover_letters: dict[str, dict[str, Any]] = {}
    has_cover_letter_rows = False
    for doc in await db.get_generated_documents_for_job(job_id):  # newest first
        lane_name = doc["search_lane"]
        if doc["doc_type"] == COVER_LETTER:
            has_cover_letter_rows = True
        if lane_name not in doc_lanes:
            continue
        pdf_path = Path(doc["file_path"])
        if doc["doc_type"] == RESUME and lane_name not in existing_resumes:
            saved = load_saved_resume(pdf_path)
            if saved is not None:
                existing_resumes[lane_name] = _resume_panel_ctx(
                    job_id, lane_name, saved, from_cache=True, cost=None,
                    model=doc["model_used"], generated_at=doc["generated_at"],
                )
        elif doc["doc_type"] == COVER_LETTER and lane_name not in existing_cover_letters:
            txt_path = pdf_path.with_suffix(".txt")
            if txt_path.exists():
                existing_cover_letters[lane_name] = {
                    "job_id": job_id, "lane": lane_name,
                    "text": txt_path.read_text(encoding="utf-8"),
                    "from_cache": True, "edited": doc["model_used"] == "manual-edit",
                    "model": doc["model_used"], "cost": None,
                    "context_source": read_context_source(txt_path), "saved": False,
                }

    return templates.TemplateResponse(request, "job_detail.html", {
        "request": request,
        "job": job,
        "lane_evals": lane_evals,
        "doc_lanes": doc_lanes,
        "existing_resumes": existing_resumes,
        "existing_cover_letters": existing_cover_letters,
        "apply_request": await db.get_latest_apply_request(job_id),
        # Legacy evaluations.cover_letter_draft only shows until letters are migrated
        "show_legacy_cover_letter": bool(job.get("cover_letter")) and not has_cover_letter_rows,
    })


# ---------------------------------------------------------------------------
# API routes (JSON + HTMX)
# ---------------------------------------------------------------------------

@app.get("/api/stats")
async def api_stats():
    """Job counts by status."""
    db = get_db()
    return await db.get_stats()


@app.get("/api/jobs")
async def api_jobs(
    status: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    sort: str = Query("date_scraped"),
    dir: str = Query("DESC"),
    lane: str | None = Query(None),
):
    """List jobs with evaluation data."""
    db = get_db()
    jobs, total = await db.get_jobs_paginated(
        status=status, limit=limit, offset=offset, sort_by=sort, sort_dir=dir, lane=lane,
    )
    return {"jobs": jobs, "total": total}


@app.get("/api/jobs/{job_id}")
async def api_job_detail(job_id: int):
    """Single job with evaluation."""
    db = get_db()
    job = await db.get_job_with_evaluation(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


class DecisionRequest(BaseModel):
    decision: str  # approved, rejected, maybe, applied
    notes: str = ""


@app.post("/api/jobs/{job_id}/decide")
async def api_decide(
    request: Request,
    job_id: int,
    decision: str | None = Form(None),
    notes: str | None = Form(None),
):
    """Record a decision on a job (approve/reject/maybe/applied).

    Accepts both form-encoded (HTMX hx-vals) and JSON request bodies.
    """
    db = get_db()

    # Parse from form data or fall back to JSON body
    if decision is not None:
        dec = decision
        dec_notes = notes or ""
    else:
        try:
            body = await request.json()
            dec = body.get("decision", "")
            dec_notes = body.get("notes", "")
        except Exception:
            raise HTTPException(status_code=400, detail="No decision provided")

    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if dec not in ("approved", "rejected", "maybe", "applied"):
        raise HTTPException(status_code=400, detail=f"Invalid decision: {dec}")

    record = DecisionRecord(
        job_id=job_id,
        decision=dec,
        notes=dec_notes,
    )
    await db.insert_decision(record)

    logger.info("Decision: job #%d → %s", job_id, dec)

    if request.headers.get("HX-Request"):
        # If the request came from the job detail page (decision-area),
        # return the updated badge partial
        hx_target = request.headers.get("HX-Target", "")
        if hx_target == "decision-area":
            job_data = await db.get_job_with_evaluation(job_id)
            return templates.TemplateResponse(request, "partials/decision_badge.html", {
                "request": request,
                "job": job_data,
            })
        # If from the job table row, return empty string to remove the row
        return HTMLResponse("")

    return {"status": "ok", "job_id": job_id, "decision": dec}


@app.post("/api/scrape")
async def api_scrape(request: Request, background_tasks: BackgroundTasks):
    """Trigger a scrape run in the background."""
    if _running_tasks["scrape"]:
        if request.headers.get("HX-Request"):
            return HTMLResponse('<span class="badge badge-yellow">Scrape already running...</span>')
        raise HTTPException(status_code=409, detail="Scrape already running")

    background_tasks.add_task(_run_scrape)

    if request.headers.get("HX-Request"):
        return HTMLResponse('<span class="badge badge-green">Scrape started!</span>')
    return {"status": "started"}


@app.post("/api/evaluate")
async def api_evaluate(request: Request, background_tasks: BackgroundTasks):
    """Trigger an evaluation run in the background."""
    if _running_tasks["evaluate"]:
        if request.headers.get("HX-Request"):
            return HTMLResponse('<span class="badge badge-yellow">Evaluation already running...</span>')
        raise HTTPException(status_code=409, detail="Evaluation already running")

    background_tasks.add_task(_run_evaluate)

    if request.headers.get("HX-Request"):
        return HTMLResponse('<span class="badge badge-green">Evaluation started!</span>')
    return {"status": "started"}


def _error_badge(request: Request, message: str, status_code: int = 400):
    # HTMX won't swap 4xx responses by default, so badges go back as 200
    if request.headers.get("HX-Request"):
        return HTMLResponse(f'<span class="badge badge-red">{message}</span>')
    raise HTTPException(status_code=status_code, detail=message)


@app.post("/api/jobs/{job_id}/generate-resume")
async def api_generate_resume(
    request: Request,
    job_id: int,
    lane: str | None = Form(None),
    force: str | None = Form(None),
):
    """Generate (or serve cached) a job-tailored resume for one lane.

    Accepts form-encoded (HTMX hx-vals) or JSON: {"lane": "...", "force": true}.
    """
    settings = get_settings()
    db = get_db()

    lane, force_flag = await _lane_and_force(request, lane, force)
    if not lane:
        return _error_badge(request, "No lane provided")

    if not settings.secrets.anthropic_api_key:
        return _error_badge(request, "API key not configured")

    from app.resume_generator.linkedin_parser import DEFAULT_DATA_PATH

    if not DEFAULT_DATA_PATH.exists():
        return _error_badge(request, "Run `job-agent parse-linkedin` first")

    from app.polisher.claude_client import ClaudeClient
    from app.resume_generator.pipeline import JobNotFoundError, ResumePipeline

    try:
        async with ClaudeClient(api_key=settings.secrets.anthropic_api_key) as claude:
            pipeline = ResumePipeline(db, claude, settings)
            result = await pipeline.generate_for_job(job_id, lane, force=force_flag)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except ValueError as e:
        return _error_badge(request, str(e))
    except Exception as e:
        logger.error("Resume generation failed for job #%d [%s]: %s", job_id, lane, e,
                     exc_info=True)
        return _error_badge(request, f"Error: {e}", status_code=500)

    gen = result.gen_result
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "partials/resume_panel.html", {
            "request": request,
            **_resume_panel_ctx(
                job_id, lane, result.resume, from_cache=result.from_cache,
                cost=gen.cost_display if gen else None,
                model=gen.model_used if gen else None,
            ),
        })

    return {
        "status": "ok",
        "resume": result.resume.model_dump(),
        "pdf_path": str(result.pdf_path),
        "docx_path": str(result.docx_path),
        "from_cache": result.from_cache,
        "cost": gen.cost_display if gen else None,
        "model": gen.model_used if gen else None,
    }


@app.get("/api/jobs/{job_id}/resume/{lane}/download")
async def api_download_resume(job_id: int, lane: str, fmt: str = Query("pdf")):
    """Download a generated resume as PDF or DOCX."""
    if fmt not in ("pdf", "docx"):
        raise HTTPException(status_code=400, detail="fmt must be pdf or docx")

    from app.resume_generator.pipeline import DOC_TYPE

    db = get_db()
    doc = await db.get_generated_document(job_id, lane, DOC_TYPE)
    if not doc:
        raise HTTPException(status_code=404, detail="No resume generated for this job/lane")
    path = Path(doc["file_path"]).with_suffix(f".{fmt}")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Resume file missing — regenerate it")

    filename = await _download_filename(db, job_id, "resume", fmt)

    media_type = (
        "application/pdf" if fmt == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    return FileResponse(path, media_type=media_type, filename=filename)


@app.post("/api/jobs/{job_id}/generate-cover-letter")
async def api_generate_cover_letter(
    request: Request,
    job_id: int,
    lane: str | None = Form(None),
    force: str | None = Form(None),
):
    """Generate (or serve cached/edited) a lane-aware cover letter.

    Accepts form-encoded (HTMX hx-vals) or JSON: {"lane": "...", "force": true}.
    """
    settings = get_settings()
    db = get_db()

    lane, force_flag = await _lane_and_force(request, lane, force)
    if not lane:
        return _error_badge(request, "No lane provided")
    if not settings.secrets.anthropic_api_key:
        return _error_badge(request, "API key not configured")

    from app.polisher.claude_client import ClaudeClient
    from app.polisher.pipeline import PolishPipeline
    from app.resume_generator.pipeline import JobNotFoundError

    try:
        async with ClaudeClient(api_key=settings.secrets.anthropic_api_key) as claude:
            pipeline = PolishPipeline(db, claude, settings)
            result = await pipeline.polish_job(job_id, lane, force=force_flag)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except (ValueError, FileNotFoundError) as e:
        return _error_badge(request, str(e))
    except Exception as e:
        logger.error("Cover letter failed for job #%d [%s]: %s", job_id, lane, e, exc_info=True)
        return _error_badge(request, f"Error: {e}", status_code=500)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "partials/cover_letter.html", {
            "request": request, **_cover_letter_panel_ctx(job_id, lane, result),
        })

    gen = result.gen_result
    return {
        "status": "ok",
        "cover_letter": result.text,
        "context_source": result.context_source,
        "from_cache": result.from_cache,
        "edited": result.edited,
        "model": result.model_used,
        "cost": gen.cost_display if gen else None,
        "txt_path": str(result.txt_path),
        "pdf_path": str(result.pdf_path),
        "docx_path": str(result.docx_path),
    }


@app.put("/api/jobs/{job_id}/cover-letter/{lane}")
async def api_save_cover_letter(
    request: Request, job_id: int, lane: str, text: str = Form(...),
):
    """Save a hand-edited cover letter and re-render its PDF/DOCX."""
    from app.polisher.pipeline import PolishPipeline
    from app.resume_generator.pipeline import JobNotFoundError

    pipeline = PolishPipeline(get_db(), None, get_settings())
    try:
        result = await pipeline.save_edited_cover_letter(job_id, lane, text)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except ValueError as e:
        return _error_badge(request, str(e))

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "partials/cover_letter.html", {
            "request": request, **_cover_letter_panel_ctx(job_id, lane, result, saved=True),
        })
    return {"status": "ok", "doc_id": result.doc_id, "model": result.model_used}


@app.get("/api/jobs/{job_id}/cover-letter/{lane}/download")
async def api_download_cover_letter(job_id: int, lane: str, fmt: str = Query("pdf")):
    """Download a cover letter as PDF, DOCX, or TXT."""
    if fmt not in ("pdf", "docx", "txt"):
        raise HTTPException(status_code=400, detail="fmt must be pdf, docx, or txt")

    from app.polisher.pipeline import DOC_TYPE

    db = get_db()
    doc = await db.get_generated_document(job_id, lane, DOC_TYPE)
    if not doc:
        raise HTTPException(status_code=404, detail="No cover letter for this job/lane")
    path = Path(doc["file_path"]).with_suffix(f".{fmt}")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Cover letter file missing — regenerate it")

    media_types = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "txt": "text/plain; charset=utf-8",
    }
    filename = await _download_filename(db, job_id, "cover_letter", fmt)
    return FileResponse(path, media_type=media_types[fmt], filename=filename)


# ---------------------------------------------------------------------------
# Apply queue — dashboard side (no token, like the rest of the dashboard)
# ---------------------------------------------------------------------------

def _elapsed(row: dict) -> str:
    """Human elapsed time for a queue row: queued/started → completed (or now)."""
    start = row.get("started_at") or row.get("queued_at")
    if not start:
        return ""
    end = row.get("completed_at")
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        t0 = datetime.strptime(start, fmt).replace(tzinfo=timezone.utc)
        t1 = (datetime.strptime(end, fmt).replace(tzinfo=timezone.utc) if end
              else datetime.now(timezone.utc))
    except ValueError:
        return ""
    minutes = int((t1 - t0).total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"


async def _apply_queue_ctx(request: Request) -> dict[str, Any]:
    rows = await get_db().get_apply_queue(limit=50)
    for row in rows:
        row["elapsed"] = _elapsed(row)
    return {
        "request": request,
        "rows": rows,
        "any_active": any(r["status"] in ("claimed", "in_progress") for r in rows),
    }


@app.get("/apply-queue", response_class=HTMLResponse)
async def apply_queue_page(request: Request):
    """Apply queue page — the table partial refreshes itself while anything is running."""
    ctx = await _apply_queue_ctx(request)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "partials/apply_queue_table.html", ctx)
    return templates.TemplateResponse(request, "apply_queue.html", ctx)


@app.post("/api/apply-queue/reset-stale")
async def api_apply_queue_reset_stale(request: Request):
    """Send claims the client abandoned back to pending."""
    count = await get_db().reset_stale_claims()
    logger.info("Reset %d stale apply claim(s) from the dashboard", count)
    ctx = await _apply_queue_ctx(request)
    ctx["flash"] = f"Reset {count} stale claim(s)"
    return templates.TemplateResponse(request, "partials/apply_queue_table.html", ctx)


@app.post("/api/jobs/{job_id}/queue-apply")
async def api_queue_apply(
    request: Request,
    job_id: int,
    lane: str | None = Form(None),
    force: str | None = Form(None),
):
    """Run pre-flight and queue the job for the apply client if it passes."""
    from app.applicator.preflight import has_blockers, preflight_and_enqueue

    lane, force_flag = await _lane_and_force(request, lane, force)
    if not lane:
        return _error_badge(request, "No lane provided")

    db = get_db()
    try:
        checks, queue_id = await preflight_and_enqueue(db, get_settings(), job_id, lane,
                                                       force=force_flag)
    except LookupError:
        raise HTTPException(status_code=404, detail="Job not found")
    except (ValueError, ApplyQueueError) as e:
        return _error_badge(request, str(e), status_code=409)

    if request.headers.get("HX-Request"):
        if queue_id is not None:
            return templates.TemplateResponse(request, "partials/apply_status.html", {
                "request": request, "apply_request": await db.get_latest_apply_request(job_id),
                "just_queued": True,
            })
        return templates.TemplateResponse(request, "partials/apply_preflight.html", {
            "request": request, "job_id": job_id, "lane": lane, "checks": checks,
            "blocked": has_blockers(checks),
        })

    return {
        "queued": queue_id is not None,
        "queue_id": queue_id,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail, "severity": c.severity}
                   for c in checks],
    }


# ---------------------------------------------------------------------------
# Apply queue — client API (Windows apply client, X-Apply-Token required)
# ---------------------------------------------------------------------------

async def require_apply_token(x_apply_token: str | None = Header(None)) -> None:
    """Shared-secret auth for the apply client. 503 until a token is configured."""
    expected = get_settings().secrets.apply_client_token
    if not expected:
        raise HTTPException(status_code=503, detail="apply_client_token not configured")
    if not x_apply_token or not secrets.compare_digest(
        x_apply_token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Apply-Token")


apply_api = APIRouter(prefix="/api/apply-queue", dependencies=[Depends(require_apply_token)])

APPLY_DOC_TYPES = {"resume": "resume_path", "cover_letter": "cover_letter_path"}


class ApplyProgress(BaseModel):
    status: str
    ats_detected: str | None = None
    apply_url: str | None = None
    fields_filled: int | None = None
    fields_flagged: int | None = None
    notes: str | None = None


class ApplyResult(BaseModel):
    status: str  # completed | abandoned | failed
    notes: str | None = None


def _applicant_payload() -> tuple[dict | None, str | None]:
    from app.applicator.applicant_data import build_applicant_data
    from app.resume_generator.linkedin_parser import load_linkedin_data

    try:
        return build_applicant_data(get_settings(), load_linkedin_data()).model_dump(), None
    except (FileNotFoundError, ValueError) as e:
        return None, str(e)


def _apply_request_payload(row: dict, applicant: dict | None,
                           applicant_error: str | None) -> dict[str, Any]:
    qid = row["id"]
    base = f"/api/apply-queue/{qid}/documents"
    return {
        "queue_id": qid,
        "job_id": row["job_id"],
        "lane": row["search_lane"],
        "status": row["status"],
        "queued_at": row["queued_at"],
        "job": {
            "title": row["job_title"],
            "company": row["job_company"],
            "url": row["job_url"],
            "source": row["job_source"],
            "description": (row["job_description"] or "")[:2000],
        },
        "applicant": applicant,
        "applicant_error": applicant_error,
        "documents": {
            doc_type: {fmt: f"{base}/{doc_type}?fmt={fmt}" for fmt in ("pdf", "docx")}
            for doc_type, col in APPLY_DOC_TYPES.items() if row.get(col)
        },
    }


@apply_api.get("/health")
async def api_apply_health():
    return {"ok": True, "pending": len(await get_db().get_pending_apply_requests())}


@apply_api.get("/pending")
async def api_apply_pending():
    rows = await get_db().get_pending_apply_requests()
    applicant, error = _applicant_payload() if rows else (None, None)
    return [_apply_request_payload(r, applicant, error) for r in rows]


@apply_api.post("/{queue_id}/claim")
async def api_apply_claim(queue_id: int):
    db = get_db()
    if await db.get_apply_request(queue_id) is None:
        raise HTTPException(status_code=404, detail="Apply request not found")
    row = await db.claim_apply_request(queue_id)
    if row is None:
        raise HTTPException(status_code=409, detail="Already claimed or not pending")
    applicant, error = _applicant_payload()
    return _apply_request_payload(row, applicant, error)


@apply_api.get("/{queue_id}/documents/{doc_type}")
async def api_apply_document(queue_id: int, doc_type: str, fmt: str = Query("pdf")):
    if doc_type not in APPLY_DOC_TYPES:
        raise HTTPException(status_code=404, detail="doc_type must be resume or cover_letter")
    if fmt not in ("pdf", "docx"):
        raise HTTPException(status_code=400, detail="fmt must be pdf or docx")
    db = get_db()
    row = await db.get_apply_request(queue_id)
    if row is None or not row.get(APPLY_DOC_TYPES[doc_type]):
        raise HTTPException(status_code=404, detail="Apply request or document not found")
    path = Path(row[APPLY_DOC_TYPES[doc_type]]).with_suffix(f".{fmt}")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document file missing on the Jetson")
    media_type = (
        "application/pdf" if fmt == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    filename = await _download_filename(db, row["job_id"], doc_type, fmt)
    return FileResponse(path, media_type=media_type, filename=filename)


@apply_api.post("/{queue_id}/progress")
async def api_apply_progress(queue_id: int, body: ApplyProgress):
    db = get_db()
    if await db.get_apply_request(queue_id) is None:
        raise HTTPException(status_code=404, detail="Apply request not found")
    try:
        await db.update_apply_progress(queue_id, **body.model_dump())
    except ApplyQueueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True}


@apply_api.post("/{queue_id}/result")
async def api_apply_result(queue_id: int, body: ApplyResult):
    db = get_db()
    if await db.get_apply_request(queue_id) is None:
        raise HTTPException(status_code=404, detail="Apply request not found")
    try:
        await db.complete_apply_request(queue_id, body.status, body.notes)
    except ApplyQueueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    row = await db.get_apply_request(queue_id)
    from app.notifier.telegram import TelegramNotifier

    notifier = TelegramNotifier.from_secrets(get_settings().secrets)
    if notifier is not None:
        try:
            async with notifier as n:
                await n.send_apply_result(
                    row["job_title"], row["job_company"], body.status,
                    row["fields_filled"], row["fields_flagged"], body.notes,
                )
        except Exception as e:
            logger.warning("Telegram apply notification failed: %s", e)
    return {"ok": True, "status": body.status}


app.include_router(apply_api)


@app.get("/api/tasks/status")
async def api_task_status():
    """Check background task status."""
    return _running_tasks


# ---------------------------------------------------------------------------
# Background task runners
# ---------------------------------------------------------------------------

async def _run_scrape():
    """Run all enabled scrapers in the background."""
    _running_tasks["scrape"] = True
    try:
        settings = get_settings()
        db = get_db()
        from app.scrapers.base import BaseScraper

        for name, board_cfg in settings.boards.boards.items():
            if not board_cfg.enabled:
                continue
            try:
                module = importlib.import_module(board_cfg.module)
                scraper_class = None
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if isinstance(attr, type) and issubclass(attr, BaseScraper) and attr is not BaseScraper:
                        scraper_class = attr
                        break
                if scraper_class is None:
                    continue

                scraper = scraper_class(board_cfg, settings.profile, db)
                async with scraper:
                    stats = await scraper.run()
                logger.info("Background scrape [%s]: %s", name, stats.summary())
            except Exception as e:
                logger.error("Background scrape [%s] failed: %s", name, e, exc_info=True)
    finally:
        _running_tasks["scrape"] = False


async def _run_evaluate():
    """Run evaluation pipeline in the background."""
    _running_tasks["evaluate"] = True
    try:
        settings = get_settings()
        db = get_db()

        from app.evaluator.ollama_client import OllamaClient
        from app.evaluator.pipeline import EvaluationPipeline

        ollama_url = settings.secrets.ollama_base_url or "http://localhost:11434"
        model = settings.secrets.ollama_model or "llama3.2:3b-instruct-q4_K_M"

        async with OllamaClient(base_url=ollama_url, model=model) as ollama:
            if not await ollama.is_healthy():
                logger.error("Ollama not reachable for background evaluation")
                return
            pipeline = EvaluationPipeline(db, ollama, settings.profile)
            stats = await pipeline.run()
            logger.info("Background evaluation: %s", stats.summary())
    except Exception as e:
        logger.error("Background evaluation failed: %s", e, exc_info=True)
    finally:
        _running_tasks["evaluate"] = False
