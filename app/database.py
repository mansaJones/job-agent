"""Database layer — async SQLite with WAL mode.

Provides the Database class that manages the connection pool, schema
migrations, and all CRUD operations for jobs, evaluations, decisions,
and applications.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models (what goes in and out of the DB)
# ---------------------------------------------------------------------------

class JobRecord(BaseModel):
    """A single job listing."""

    id: int | None = None
    source: str
    external_id: str | None = None
    url: str
    title: str
    company: str | None = None
    location: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    description: str | None = None
    raw_html: str | None = None
    date_posted: str | None = None
    date_scraped: str | None = None
    status: str = "new"
    rejection_reason: str | None = None


class EvaluationRecord(BaseModel):
    """LLM evaluation of a job."""

    id: int | None = None
    job_id: int
    model_used: str
    match_score: float | None = None
    reasoning: str | None = None
    cover_letter_draft: str | None = None
    evaluated_at: str | None = None


class DecisionRecord(BaseModel):
    """User decision on a job."""

    id: int | None = None
    job_id: int
    decision: str  # 'approved', 'rejected', 'maybe'
    notes: str | None = None
    decided_at: str | None = None


class AppliedRecord(BaseModel):
    """Record of a job application."""

    id: int | None = None
    job_id: int
    method: str | None = None  # 'manual', 'assisted'
    applied_at: str | None = None
    follow_up_date: str | None = None


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    salary_min REAL,
    salary_max REAL,
    description TEXT,
    raw_html TEXT,
    date_posted TEXT,
    date_scraped TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'new',
    rejection_reason TEXT
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    model_used TEXT NOT NULL,
    match_score REAL,
    reasoning TEXT,
    cover_letter_draft TEXT,
    evaluated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    decision TEXT NOT NULL,
    notes TEXT,
    decided_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS applied (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    method TEXT,
    applied_at TEXT DEFAULT (datetime('now')),
    follow_up_date TEXT
);

CREATE TABLE IF NOT EXISTS scraper_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    run_at TEXT DEFAULT (datetime('now')),
    pages_fetched INTEGER DEFAULT 0,
    pages_blocked INTEGER DEFAULT 0,
    pages_failed INTEGER DEFAULT 0,
    jobs_found INTEGER DEFAULT 0,
    jobs_parsed INTEGER DEFAULT 0,
    parse_errors INTEGER DEFAULT 0,
    playwright_used INTEGER DEFAULT 0,
    delay_min_used REAL,
    delay_max_used REAL,
    fetch_strategy TEXT DEFAULT 'httpx',
    notes TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_ext
    ON jobs(source, external_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status
    ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_evaluations_score
    ON evaluations(match_score);
CREATE INDEX IF NOT EXISTS idx_scraper_health_source
    ON scraper_health(source, run_at);
"""


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class Database:
    """Async SQLite database manager.

    Usage:
        db = Database("/path/to/jobs.db")
        await db.initialize()
        # ... use it ...
        await db.close()

    Or as an async context manager:
        async with Database("/path/to/jobs.db") as db:
            ...
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open connection, enable WAL mode, create schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row

        # WAL mode for better concurrent read/write performance
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")

        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

        # Migrations — add columns that may not exist in older DBs
        await self._run_migrations()

        logger.info("Database initialized at %s (WAL mode)", self.db_path)

    async def _run_migrations(self) -> None:
        """Apply schema migrations for columns added after initial release."""
        migrations = [
            ("jobs", "rejection_reason", "ALTER TABLE jobs ADD COLUMN rejection_reason TEXT"),
        ]
        for table, column, ddl in migrations:
            cursor = await self.conn.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in await cursor.fetchall()]
            if column not in columns:
                await self.conn.execute(ddl)
                await self.conn.commit()
                logger.info("Migration: added %s.%s", table, column)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Database connection closed")

    async def __aenter__(self) -> Database:
        await self.initialize()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        return self._conn

    # ------------------------------------------------------------------
    # Jobs CRUD
    # ------------------------------------------------------------------

    async def insert_job(self, job: JobRecord) -> int | None:
        """Insert a job, returning the new row ID. Returns None if duplicate URL."""
        try:
            cursor = await self.conn.execute(
                """
                INSERT INTO jobs (source, external_id, url, title, company, location,
                                  salary_min, salary_max, description, raw_html,
                                  date_posted, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.source, job.external_id, job.url, job.title, job.company,
                    job.location, job.salary_min, job.salary_max, job.description,
                    job.raw_html, job.date_posted, job.status,
                ),
            )
            await self.conn.commit()
            logger.debug("Inserted job: %s — %s", job.title, job.company)
            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            logger.debug("Duplicate job skipped: %s", job.url)
            return None

    async def get_job(self, job_id: int) -> JobRecord | None:
        """Fetch a single job by ID."""
        cursor = await self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return JobRecord(**dict(row))

    async def get_jobs_by_status(
        self,
        status: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JobRecord]:
        """Fetch jobs by status, newest first."""
        cursor = await self.conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY date_scraped DESC LIMIT ? OFFSET ?",
            (status, limit, offset),
        )
        rows = await cursor.fetchall()
        return [JobRecord(**dict(r)) for r in rows]

    async def get_new_jobs(self, limit: int = 100) -> list[JobRecord]:
        """Convenience: fetch jobs with status 'new'."""
        return await self.get_jobs_by_status("new", limit=limit)

    async def update_job_status(
        self, job_id: int, status: str, rejection_reason: str | None = None
    ) -> None:
        """Update the status of a job, optionally storing a rejection reason."""
        if rejection_reason:
            await self.conn.execute(
                "UPDATE jobs SET status = ?, rejection_reason = ? WHERE id = ?",
                (status, rejection_reason, job_id),
            )
        else:
            await self.conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ?", (status, job_id)
            )
        await self.conn.commit()

    async def job_url_exists(self, url: str) -> bool:
        """Check if a job URL is already in the database."""
        cursor = await self.conn.execute(
            "SELECT 1 FROM jobs WHERE url = ? LIMIT 1", (url,)
        )
        return await cursor.fetchone() is not None

    async def count_jobs(self, status: str | None = None) -> int:
        """Count jobs, optionally filtered by status."""
        if status:
            cursor = await self.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)
            )
        else:
            cursor = await self.conn.execute("SELECT COUNT(*) FROM jobs")
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Evaluations CRUD
    # ------------------------------------------------------------------

    async def insert_evaluation(self, evaluation: EvaluationRecord) -> int:
        """Insert an evaluation record."""
        cursor = await self.conn.execute(
            """
            INSERT INTO evaluations (job_id, model_used, match_score, reasoning,
                                     cover_letter_draft)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                evaluation.job_id, evaluation.model_used, evaluation.match_score,
                evaluation.reasoning, evaluation.cover_letter_draft,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_evaluation(self, job_id: int) -> EvaluationRecord | None:
        """Get the latest evaluation for a job."""
        cursor = await self.conn.execute(
            "SELECT * FROM evaluations WHERE job_id = ? ORDER BY evaluated_at DESC LIMIT 1",
            (job_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return EvaluationRecord(**dict(row))

    # ------------------------------------------------------------------
    # Decisions CRUD
    # ------------------------------------------------------------------

    async def insert_decision(self, decision: DecisionRecord) -> int:
        """Record a user decision on a job."""
        cursor = await self.conn.execute(
            "INSERT INTO decisions (job_id, decision, notes) VALUES (?, ?, ?)",
            (decision.job_id, decision.decision, decision.notes),
        )
        await self.conn.commit()

        # Also update the job status to match the decision
        status_map = {"approved": "approved", "rejected": "rejected", "maybe": "evaluated"}
        new_status = status_map.get(decision.decision, decision.decision)
        await self.update_job_status(decision.job_id, new_status)

        return cursor.lastrowid  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Applied CRUD
    # ------------------------------------------------------------------

    async def insert_applied(self, applied: AppliedRecord) -> int:
        """Record that you applied to a job."""
        cursor = await self.conn.execute(
            "INSERT INTO applied (job_id, method, follow_up_date) VALUES (?, ?, ?)",
            (applied.job_id, applied.method, applied.follow_up_date),
        )
        await self.conn.commit()
        await self.update_job_status(applied.job_id, "applied")
        return cursor.lastrowid  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict[str, int]:
        """Get counts of jobs by status."""
        cursor = await self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
        )
        rows = await cursor.fetchall()
        stats = {row["status"]: row["cnt"] for row in rows}
        stats["total"] = sum(stats.values())
        return stats

    # ------------------------------------------------------------------
    # Dashboard queries
    # ------------------------------------------------------------------

    async def get_jobs_paginated(
        self,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
        sort_by: str = "date_scraped",
        sort_dir: str = "DESC",
    ) -> tuple[list[dict], int]:
        """Fetch jobs with evaluation data, paginated. Returns (rows, total_count).

        Each row is a dict with all job fields plus eval_score and eval_reasoning.
        """
        # Whitelist sort columns to prevent injection
        allowed_sorts = {
            "date_scraped", "title", "company", "status", "eval_score",
        }
        if sort_by not in allowed_sorts:
            sort_by = "date_scraped"
        if sort_dir.upper() not in ("ASC", "DESC"):
            sort_dir = "DESC"

        where_clause = "WHERE j.status = ?" if status else ""
        params: list = [status] if status else []

        # Total count
        count_sql = f"SELECT COUNT(*) FROM jobs j {where_clause}"
        cursor = await self.conn.execute(count_sql, params)
        row = await cursor.fetchone()
        total = row[0] if row else 0

        # Handle eval_score sort (it's from the join)
        order_col = "e.match_score" if sort_by == "eval_score" else f"j.{sort_by}"

        query = f"""
            SELECT j.*,
                   e.match_score AS eval_score,
                   e.reasoning AS eval_reasoning,
                   e.model_used AS eval_model
            FROM jobs j
            LEFT JOIN evaluations e ON e.job_id = j.id
                AND e.evaluated_at = (
                    SELECT MAX(e2.evaluated_at) FROM evaluations e2 WHERE e2.job_id = j.id
                )
            {where_clause}
            ORDER BY {order_col} {sort_dir}
            LIMIT ? OFFSET ?
        """
        cursor = await self.conn.execute(query, params + [limit, offset])
        rows = await cursor.fetchall()
        return [dict(r) for r in rows], total

    async def get_job_with_evaluation(self, job_id: int) -> dict | None:
        """Fetch a single job with its latest evaluation data."""
        cursor = await self.conn.execute(
            """
            SELECT j.*,
                   e.match_score AS eval_score,
                   e.reasoning AS eval_reasoning,
                   e.model_used AS eval_model,
                   e.evaluated_at AS eval_date,
                   e.cover_letter_draft AS cover_letter
            FROM jobs j
            LEFT JOIN evaluations e ON e.job_id = j.id
                AND e.evaluated_at = (
                    SELECT MAX(e2.evaluated_at) FROM evaluations e2 WHERE e2.job_id = j.id
                )
            WHERE j.id = ?
            """,
            (job_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
