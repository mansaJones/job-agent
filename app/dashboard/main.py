"""FastAPI dashboard — web UI for reviewing and managing job matches.

Serves HTMX-powered HTML pages + JSON API endpoints.
Run with: job-agent dashboard
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import load_settings, AppSettings
from app.database import Database, DecisionRecord
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


def _resume_lanes(job: dict) -> list[str]:
    """Lanes a resume can be generated for: every enabled lane for 'both', else the job's lane."""
    lanes = _enabled_lane_names()
    if job.get("search_lane") == "both":
        return lanes
    if job.get("search_lane") in lanes:
        return [job["search_lane"]]
    return lanes[:1]


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

    # Previously generated resumes render populated instead of the opt-in stub
    from app.resume_generator.pipeline import DOC_TYPE, load_saved_resume

    resume_lanes = _resume_lanes(job)
    existing_resumes: dict[str, dict[str, Any]] = {}
    for lane_name in resume_lanes:
        doc = await db.get_generated_document(job_id, lane_name, DOC_TYPE)
        if not doc:
            continue
        saved = load_saved_resume(Path(doc["file_path"]))
        if saved is None:
            continue
        existing_resumes[lane_name] = _resume_panel_ctx(
            job_id, lane_name, saved, from_cache=True, cost=None,
            model=doc["model_used"], generated_at=doc["generated_at"],
        )

    return templates.TemplateResponse(request, "job_detail.html", {
        "request": request,
        "job": job,
        "lane_evals": lane_evals,
        "resume_lanes": resume_lanes,
        "existing_resumes": existing_resumes,
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


@app.post("/api/jobs/{job_id}/polish")
async def api_polish(request: Request, job_id: int):
    """Generate a cover letter for a job using Claude API."""
    settings = get_settings()
    db = get_db()

    if not settings.secrets.anthropic_api_key:
        if request.headers.get("HX-Request"):
            return HTMLResponse('<span class="badge badge-red">API key not configured</span>')
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")

    job = await db.get_job_with_evaluation(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Run synchronously (cover letters are fast, ~2-5s)
    from app.polisher.claude_client import ClaudeClient
    from app.polisher.pipeline import PolishPipeline

    try:
        async with ClaudeClient(api_key=settings.secrets.anthropic_api_key) as claude:
            pipeline = PolishPipeline(db, claude, settings)
            result = await pipeline.polish_job(job_id)
    except Exception as e:
        logger.error("Polish failed for job #%d: %s", job_id, e, exc_info=True)
        if request.headers.get("HX-Request"):
            return HTMLResponse(f'<span class="badge badge-red">Error: {e}</span>')
        raise HTTPException(status_code=500, detail=str(e))

    if result is None:
        if request.headers.get("HX-Request"):
            return HTMLResponse('<span class="badge badge-red">Generation failed</span>')
        raise HTTPException(status_code=500, detail="Cover letter generation failed")

    if request.headers.get("HX-Request"):
        # Return the cover letter section for HTMX swap
        return templates.TemplateResponse(request, "partials/cover_letter.html", {
            "request": request,
            "cover_letter": result.cover_letter,
            "cost": result.cost_display,
            "model": result.model_used,
        })

    return {
        "status": "ok",
        "cover_letter": result.cover_letter,
        "cost": result.cost_display,
        "model": result.model_used,
    }


def _resume_error(request: Request, message: str, status_code: int = 400):
    # HTMX won't swap 4xx responses by default, so badges go back as 200 (like /polish)
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

    force_flag = str(force).lower() in ("true", "1", "yes", "on") if force is not None else False
    if lane is None:
        try:
            body = await request.json()
            lane = body.get("lane")
            force_flag = bool(body.get("force", False))
        except Exception:
            lane = None
    if not lane:
        return _resume_error(request, "No lane provided")

    if not settings.secrets.anthropic_api_key:
        return _resume_error(request, "API key not configured")

    from app.resume_generator.linkedin_parser import DEFAULT_DATA_PATH

    if not DEFAULT_DATA_PATH.exists():
        return _resume_error(request, "Run `job-agent parse-linkedin` first")

    from app.polisher.claude_client import ClaudeClient
    from app.resume_generator.pipeline import JobNotFoundError, ResumePipeline

    try:
        async with ClaudeClient(api_key=settings.secrets.anthropic_api_key) as claude:
            pipeline = ResumePipeline(db, claude, settings)
            result = await pipeline.generate_for_job(job_id, lane, force=force_flag)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except ValueError as e:
        return _resume_error(request, str(e))
    except Exception as e:
        logger.error("Resume generation failed for job #%d [%s]: %s", job_id, lane, e,
                     exc_info=True)
        return _resume_error(request, f"Error: {e}", status_code=500)

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

    from app.resume_generator.linkedin_parser import load_linkedin_data
    from app.resume_generator.pipeline import DOC_TYPE

    db = get_db()
    doc = await db.get_generated_document(job_id, lane, DOC_TYPE)
    if not doc:
        raise HTTPException(status_code=404, detail="No resume generated for this job/lane")
    path = Path(doc["file_path"]).with_suffix(f".{fmt}")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Resume file missing — regenerate it")

    job = await db.get_job(job_id)
    company_slug = re.sub(r"[^A-Za-z0-9]+", "_", (job.company if job else "") or "").strip("_")
    try:
        last_name = load_linkedin_data().last_name or "resume"
    except FileNotFoundError:
        last_name = "resume"
    filename = f"{last_name}_{company_slug or 'job'}_resume.{fmt}"

    media_type = (
        "application/pdf" if fmt == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    return FileResponse(path, media_type=media_type, filename=filename)


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
