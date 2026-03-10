"""FastAPI dashboard — web UI for reviewing and managing job matches.

Serves HTMX-powered HTML pages + JSON API endpoints.
Run with: job-agent dashboard
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
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


# Register filters for Jinja2
templates.env.filters["score_color"] = _score_color
templates.env.filters["format_salary"] = lambda row: _format_salary(
    row.get("salary_min"), row.get("salary_max")
)
templates.env.globals["score_color"] = _score_color
templates.env.globals["format_salary"] = _format_salary


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

    return templates.TemplateResponse("index.html", {
        "request": request,
        "stats": stats,
        "top_matches": top_matches,
        "recent_maybes": recent_maybes,
        "running_tasks": _running_tasks,
    })


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(
    request: Request,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    sort: str = Query("date_scraped"),
    dir: str = Query("DESC"),
):
    """Job list page with filters."""
    db = get_db()
    offset = (page - 1) * limit
    jobs, total = await db.get_jobs_paginated(
        status=status, limit=limit, offset=offset, sort_by=sort, sort_dir=dir
    )
    total_pages = max(1, (total + limit - 1) // limit)
    all_stats = await db.get_stats()

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
        "stats": all_stats,
    }

    # If HTMX request, return just the table partial
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/job_table.html", ctx)

    return templates.TemplateResponse("jobs.html", ctx)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail_page(request: Request, job_id: int):
    """Job detail page."""
    db = get_db()
    job = await db.get_job_with_evaluation(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return templates.TemplateResponse("job_detail.html", {
        "request": request,
        "job": job,
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
):
    """List jobs with evaluation data."""
    db = get_db()
    jobs, total = await db.get_jobs_paginated(
        status=status, limit=limit, offset=offset, sort_by=sort, sort_dir=dir,
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
    decision: str  # approved, rejected, maybe
    notes: str = ""


@app.post("/api/jobs/{job_id}/decide")
async def api_decide(request: Request, job_id: int, body: DecisionRequest):
    """Record a decision on a job (approve/reject/maybe)."""
    db = get_db()

    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if body.decision not in ("approved", "rejected", "maybe"):
        raise HTTPException(status_code=400, detail="Invalid decision")

    record = DecisionRecord(
        job_id=job_id,
        decision=body.decision,
        notes=body.notes,
    )
    await db.insert_decision(record)

    logger.info("Decision: job #%d → %s", job_id, body.decision)

    # If HTMX, return the updated badge partial
    if request.headers.get("HX-Request"):
        job_data = await db.get_job_with_evaluation(job_id)
        return templates.TemplateResponse("partials/decision_badge.html", {
            "request": request,
            "job": job_data,
        })

    return {"status": "ok", "job_id": job_id, "decision": body.decision}


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
