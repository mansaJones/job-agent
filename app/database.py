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
    search_lane: str | None = None  # lane name, or 'both' if found by multiple lanes


class EvaluationRecord(BaseModel):
    """LLM evaluation of a job."""

    id: int | None = None
    job_id: int
    model_used: str
    match_score: float | None = None
    reasoning: str | None = None
    cover_letter_draft: str | None = None
    evaluated_at: str | None = None
    search_lane: str | None = None


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
    rejection_reason TEXT,
    search_lane TEXT DEFAULT 'frontend_developer'
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    model_used TEXT NOT NULL,
    match_score REAL,
    reasoning TEXT,
    cover_letter_draft TEXT,
    evaluated_at TEXT DEFAULT (datetime('now')),
    search_lane TEXT
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

CREATE TABLE IF NOT EXISTS generated_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    search_lane TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    model_used TEXT,
    generated_at TEXT DEFAULT (datetime('now')),
    content_hash TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS apply_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'pending',
    resume_doc_id INTEGER REFERENCES generated_documents(id),
    cover_letter_doc_id INTEGER REFERENCES generated_documents(id),
    queued_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
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
CREATE INDEX IF NOT EXISTS idx_generated_docs_job
    ON generated_documents(job_id);
CREATE INDEX IF NOT EXISTS idx_apply_queue_status
    ON apply_queue(status);
"""

# Indexes on columns added by migrations — created after _run_migrations(),
# since SCHEMA_SQL runs first and the column may not exist yet on older DBs.
POST_MIGRATION_SQL = """
CREATE INDEX IF NOT EXISTS idx_jobs_lane
    ON jobs(search_lane);
"""

# Correlated subquery (references outer `j`) picking the evaluation to show for
# a job: the latest eval per lane, then the highest-scoring of those. A job
# tagged 'both' is classified by its best lane, so that's the score to display.
_BEST_EVAL_ID_SQL = """
    SELECT e2.id FROM evaluations e2
    WHERE e2.id IN (
        SELECT MAX(e3.id) FROM evaluations e3
        WHERE e3.job_id = j.id GROUP BY e3.search_lane
    )
    ORDER BY e2.match_score DESC, e2.id DESC
    LIMIT 1
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
        await self._conn.execute("PRAGMA busy_timeout=30000")  # 30s — eval holds locks for a while

        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

        # Migrations — add columns that may not exist in older DBs
        await self._run_migrations()

        logger.info("Database initialized at %s (WAL mode)", self.db_path)

    async def _run_migrations(self) -> None:
        """Apply schema migrations for columns added after initial release."""
        migrations = [
            ("jobs", "rejection_reason", "ALTER TABLE jobs ADD COLUMN rejection_reason TEXT"),
            ("jobs", "search_lane",
             "ALTER TABLE jobs ADD COLUMN search_lane TEXT DEFAULT 'frontend_developer'"),
            ("evaluations", "search_lane", "ALTER TABLE evaluations ADD COLUMN search_lane TEXT"),
            ("generated_documents", "updated_at",
             "ALTER TABLE generated_documents ADD COLUMN updated_at TEXT"),
        ]
        for table, column, ddl in migrations:
            cursor = await self.conn.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in await cursor.fetchall()]
            if column not in columns:
                await self.conn.execute(ddl)
                await self.conn.commit()
                logger.info("Migration: added %s.%s", table, column)

        await self.conn.executescript(POST_MIGRATION_SQL)
        await self.conn.commit()

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
                                  date_posted, status, search_lane)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.source, job.external_id, job.url, job.title, job.company,
                    job.location, job.salary_min, job.salary_max, job.description,
                    job.raw_html, job.date_posted, job.status, job.search_lane,
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

    async def add_lane_to_job(self, url: str, lane: str) -> bool:
        """Record that an existing job was also found by another search lane.

        If the job currently belongs to a different single lane, it's promoted
        to 'both'. Already 'both' or same lane → no-op.

        Returns True if the job was promoted to 'both'.
        """
        cursor = await self.conn.execute(
            "SELECT id, search_lane FROM jobs WHERE url = ? LIMIT 1", (url,)
        )
        row = await cursor.fetchone()
        if row is None:
            return False

        current = row["search_lane"]
        if current == lane or current == "both":
            return False

        if current is None:
            # Legacy row with no lane — just claim it for this lane
            await self.conn.execute(
                "UPDATE jobs SET search_lane = ? WHERE id = ?", (lane, row["id"])
            )
            await self.conn.commit()
            return False

        await self.conn.execute(
            "UPDATE jobs SET search_lane = 'both' WHERE id = ?", (row["id"],)
        )
        await self.conn.commit()
        logger.debug("Job #%d tagged 'both' (%s + %s)", row["id"], current, lane)
        return True

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
                                     cover_letter_draft, search_lane)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation.job_id, evaluation.model_used, evaluation.match_score,
                evaluation.reasoning, evaluation.cover_letter_draft, evaluation.search_lane,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_evaluation(
        self, job_id: int, lane: str | None = None
    ) -> EvaluationRecord | None:
        """Get the latest evaluation for a job, optionally for a specific lane."""
        if lane:
            cursor = await self.conn.execute(
                "SELECT * FROM evaluations WHERE job_id = ? AND search_lane = ? "
                "ORDER BY evaluated_at DESC, id DESC LIMIT 1",
                (job_id, lane),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM evaluations WHERE job_id = ? "
                "ORDER BY evaluated_at DESC, id DESC LIMIT 1",
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
        status_map = {"approved": "approved", "rejected": "rejected", "maybe": "maybe", "applied": "applied"}
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
    # Generated documents CRUD
    # ------------------------------------------------------------------

    async def insert_generated_document(
        self,
        job_id: int,
        search_lane: str,
        doc_type: str,
        file_path: str,
        model_used: str | None,
        content_hash: str | None,
    ) -> int:
        """Record a generated document (resume, cover letter) for a job + lane."""
        cursor = await self.conn.execute(
            """
            INSERT INTO generated_documents (job_id, search_lane, doc_type, file_path,
                                             model_used, content_hash, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (job_id, search_lane, doc_type, file_path, model_used, content_hash),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_generated_document(
        self, job_id: int, search_lane: str, doc_type: str
    ) -> dict | None:
        """Latest generated document for a job + lane + type."""
        cursor = await self.conn.execute(
            "SELECT * FROM generated_documents "
            "WHERE job_id = ? AND search_lane = ? AND doc_type = ? "
            "ORDER BY id DESC LIMIT 1",
            (job_id, search_lane, doc_type),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_generated_document(
        self, doc_id: int, content_hash: str | None, model_used: str | None
    ) -> None:
        """Update a generated document's hash/model in place (e.g. after a manual edit)."""
        await self.conn.execute(
            "UPDATE generated_documents SET content_hash = ?, model_used = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (content_hash, model_used, doc_id),
        )
        await self.conn.commit()

    async def get_generated_documents_for_job(self, job_id: int) -> list[dict]:
        """All generated documents for a job — any lane, any type — newest first."""
        cursor = await self.conn.execute(
            "SELECT * FROM generated_documents WHERE job_id = ? ORDER BY id DESC",
            (job_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def delete_generated_document(
        self, job_id: int, search_lane: str, doc_type: str
    ) -> None:
        """Delete generated document rows for a job + lane + type (files are left alone)."""
        await self.conn.execute(
            "DELETE FROM generated_documents "
            "WHERE job_id = ? AND search_lane = ? AND doc_type = ?",
            (job_id, search_lane, doc_type),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_stats(self, lane: str | None = None) -> dict[str, Any]:
        """Get counts of jobs by status, plus per-lane counts under 'by_lane'.

        If lane is given, status counts are scoped to that lane (including
        'both' jobs); 'by_lane' always covers every job.
        """
        if lane:
            cursor = await self.conn.execute(
                "SELECT status, COUNT(*) as cnt FROM jobs "
                "WHERE search_lane = ? OR search_lane = 'both' GROUP BY status",
                (lane,),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
            )
        rows = await cursor.fetchall()
        stats: dict[str, Any] = {row["status"]: row["cnt"] for row in rows}
        stats["total"] = sum(stats.values())

        cursor = await self.conn.execute(
            "SELECT search_lane, COUNT(*) as cnt FROM jobs GROUP BY search_lane"
        )
        rows = await cursor.fetchall()
        stats["by_lane"] = {(row["search_lane"] or "unassigned"): row["cnt"] for row in rows}
        return stats

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def purge_stale_listings(
        self, max_age_days: int = 30, preserve_statuses: list[str] | None = None
    ) -> int:
        """Delete jobs scraped more than max_age_days ago, unless their status is preserved.

        Child rows in evaluations/decisions/applied are deleted explicitly — those
        tables were created without ON DELETE CASCADE and SQLite can't alter FK
        constraints in place. generated_documents/apply_queue cascade on their own.

        Returns the number of jobs deleted.
        """
        if preserve_statuses is None:
            preserve_statuses = ["approved", "applied"]

        params: list[Any] = [f"-{int(max_age_days)} days"]
        status_filter = ""
        if preserve_statuses:
            placeholders = ", ".join("?" for _ in preserve_statuses)
            status_filter = f"AND status NOT IN ({placeholders})"
            params.extend(preserve_statuses)

        cursor = await self.conn.execute(
            f"SELECT id FROM jobs WHERE date_scraped < datetime('now', ?) {status_filter}",
            params,
        )
        job_ids = [row[0] for row in await cursor.fetchall()]
        if not job_ids:
            logger.info("Stale listing purge: nothing older than %d days", max_age_days)
            return 0

        chunk_size = 500  # stay well under SQLite's bound-variable limit
        try:
            await self.conn.execute("BEGIN")
            for i in range(0, len(job_ids), chunk_size):
                chunk = job_ids[i:i + chunk_size]
                placeholders = ", ".join("?" for _ in chunk)
                for table in ("evaluations", "decisions", "applied"):
                    await self.conn.execute(
                        f"DELETE FROM {table} WHERE job_id IN ({placeholders})", chunk
                    )
                await self.conn.execute(
                    f"DELETE FROM jobs WHERE id IN ({placeholders})", chunk
                )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise

        logger.info(
            "Stale listing purge: deleted %d jobs older than %d days (preserved: %s)",
            len(job_ids), max_age_days, ", ".join(preserve_statuses) or "none",
        )
        return len(job_ids)

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
        lane: str | None = None,
    ) -> tuple[list[dict], int]:
        """Fetch jobs with evaluation data, paginated. Returns (rows, total_count).

        Each row is a dict with all job fields plus eval_score, eval_reasoning,
        and eval_lane. When lane is set, jobs tagged 'both' are included too.
        """
        # Whitelist sort columns to prevent injection
        allowed_sorts = {
            "date_scraped", "title", "company", "status", "eval_score",
        }
        if sort_by not in allowed_sorts:
            sort_by = "date_scraped"
        if sort_dir.upper() not in ("ASC", "DESC"):
            sort_dir = "DESC"

        conditions: list[str] = []
        params: list = []
        if status:
            conditions.append("j.status = ?")
            params.append(status)
        if lane:
            conditions.append("(j.search_lane = ? OR j.search_lane = 'both')")
            params.append(lane)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # Total count
        count_sql = f"SELECT COUNT(*) FROM jobs j {where_clause}"
        cursor = await self.conn.execute(count_sql, params)
        row = await cursor.fetchone()
        total = row[0] if row else 0

        # Handle eval_score sort (it's from the join)
        order_col = "e.match_score" if sort_by == "eval_score" else f"j.{sort_by}"

        query = f"""
            SELECT j.*,
                   j.search_lane,
                   e.match_score AS eval_score,
                   e.reasoning AS eval_reasoning,
                   e.model_used AS eval_model,
                   e.search_lane AS eval_lane
            FROM jobs j
            LEFT JOIN evaluations e ON e.id = ({_BEST_EVAL_ID_SQL})
            {where_clause}
            ORDER BY {order_col} {sort_dir}
            LIMIT ? OFFSET ?
        """
        cursor = await self.conn.execute(query, params + [limit, offset])
        rows = await cursor.fetchall()
        return [dict(r) for r in rows], total

    async def get_job_with_evaluation(self, job_id: int) -> dict | None:
        """Fetch a single job with its best current evaluation data."""
        cursor = await self.conn.execute(
            f"""
            SELECT j.*,
                   e.match_score AS eval_score,
                   e.reasoning AS eval_reasoning,
                   e.model_used AS eval_model,
                   e.evaluated_at AS eval_date,
                   e.search_lane AS eval_lane,
                   (SELECT e4.cover_letter_draft FROM evaluations e4
                     WHERE e4.job_id = j.id AND e4.cover_letter_draft IS NOT NULL
                     ORDER BY e4.id DESC LIMIT 1) AS cover_letter
            FROM jobs j
            LEFT JOIN evaluations e ON e.id = ({_BEST_EVAL_ID_SQL})
            WHERE j.id = ?
            """,
            (job_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
