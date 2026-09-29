"""Standalone tests for the job agent core — runs with stdlib only.

Tests the database schema, config YAML parsing, salary parser, and blacklist
logic with stdlib only. The search lane, stale-listing purge, and per-lane
eval prompt tests import `app`, so run with the project venv.

Run: python3 tests/test_core.py
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import textwrap
import yaml
from pathlib import Path

# Make `app` importable when run as `python tests/test_core.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "PASS" if condition else "FAIL"
    suffix = f" ({detail})" if detail else ""
    print(f"  [{status}] {name}{suffix}")
    if condition:
        PASS += 1
    else:
        FAIL += 1


# =====================================================================
# Database tests
# =====================================================================

def test_database() -> None:
    print("\n=== Database Layer ===")
    db_path = "/tmp/test_job_agent.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # WAL mode
    result = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    check("WAL mode enabled", result[0] == "wal", f"mode={result[0]}")

    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")

    # Schema — read from the actual source file
    schema_path = Path(__file__).parent.parent / "app" / "database.py"
    source = schema_path.read_text()

    # Extract SCHEMA_SQL string
    match = re.search(r'SCHEMA_SQL\s*=\s*"""(.+?)"""', source, re.DOTALL)
    assert match, "Could not find SCHEMA_SQL in database.py"
    schema_sql = match.group(1)

    conn.executescript(schema_sql)
    conn.commit()

    # Verify all tables exist
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    expected_tables = ["applied", "apply_queue", "decisions", "evaluations",
                       "generated_documents", "jobs"]
    check("All tables created", all(t in tables for t in expected_tables), str(tables))

    # Verify indexes exist
    indexes = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    ).fetchall()]
    check("Indexes created", len(indexes) >= 3, str(indexes))

    # --- Insert jobs ---
    conn.execute(
        "INSERT INTO jobs (source, external_id, url, title, company, location, "
        "salary_min, salary_max, description, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("indeed", "abc123", "https://indeed.com/viewjob?jk=abc123",
         "DevOps Engineer", "Acme Corp", "Chicago, IL",
         90000, 120000, "A great DevOps role", "new"),
    )
    conn.commit()

    # Duplicate URL should fail
    try:
        conn.execute(
            "INSERT INTO jobs (source, external_id, url, title, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("indeed", "abc123", "https://indeed.com/viewjob?jk=abc123",
             "Dupe", "new"),
        )
        conn.commit()
        check("Duplicate URL rejected", False)
    except sqlite3.IntegrityError:
        check("Duplicate URL rejected", True)

    # Composite unique index (source + external_id)
    try:
        conn.execute(
            "INSERT INTO jobs (source, external_id, url, title, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("indeed", "abc123", "https://other-url.com", "Dupe ExtID", "new"),
        )
        conn.commit()
        check("Composite unique index (source+ext_id)", False)
    except sqlite3.IntegrityError:
        check("Composite unique index (source+ext_id)", True)

    # Insert second job for further tests
    conn.execute(
        "INSERT INTO jobs (source, external_id, url, title, company, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("indeed", "def456", "https://indeed.com/viewjob?jk=def456",
         "Cloud Engineer", "BigCo", "new"),
    )
    conn.commit()

    # Status count query
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
    ).fetchall()
    stats = {r["status"]: r["cnt"] for r in rows}
    check("Status count query", stats.get("new") == 2, str(dict(stats)))

    # Foreign key enforcement
    fk_check = conn.execute("PRAGMA foreign_keys").fetchone()
    check("Foreign keys pragma ON", fk_check[0] == 1)

    try:
        conn.execute(
            "INSERT INTO evaluations (job_id, model_used, match_score) "
            "VALUES (?, ?, ?)",
            (999, "test-model", 0.5),
        )
        conn.commit()
        check("FK rejects invalid job_id", False)
    except sqlite3.IntegrityError:
        check("FK rejects invalid job_id", True)

    # Valid evaluation insert
    conn.execute(
        "INSERT INTO evaluations (job_id, model_used, match_score, reasoning) "
        "VALUES (?, ?, ?, ?)",
        (1, "llama3.1:8b-q4", 0.85, "Strong DevOps + AWS match"),
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM evaluations WHERE job_id = 1 "
        "ORDER BY evaluated_at DESC LIMIT 1"
    ).fetchone()
    check("Evaluation insert + retrieve", row["match_score"] == 0.85)
    check("Evaluation has timestamp", row["evaluated_at"] is not None)

    # Decision + status update
    conn.execute(
        "INSERT INTO decisions (job_id, decision, notes) VALUES (?, ?, ?)",
        (1, "approved", "Looks solid"),
    )
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", ("approved", 1))
    conn.commit()

    job = conn.execute("SELECT * FROM jobs WHERE id = 1").fetchone()
    check("Decision flow — status updated", job["status"] == "approved")

    # Applied record
    conn.execute(
        "INSERT INTO applied (job_id, method) VALUES (?, ?)",
        (1, "manual"),
    )
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", ("applied", 1))
    conn.commit()

    job = conn.execute("SELECT * FROM jobs WHERE id = 1").fetchone()
    check("Applied record — status cascade", job["status"] == "applied")

    conn.close()
    os.remove(db_path)


# =====================================================================
# Config tests
# =====================================================================

def test_config() -> None:
    print("\n=== Config Parsing ===")
    config_dir = Path(__file__).parent.parent / "config"

    # Profile YAML
    profile_path = config_dir / "profile.yaml"
    check("profile.yaml exists", profile_path.exists())

    with open(profile_path) as f:
        profile = yaml.safe_load(f)

    lanes = profile.get("search_lanes", {})
    check("Has search_lanes", len(lanes) >= 2, str(list(lanes)))
    check("No top-level target_roles/skills (moved into lanes)",
          "target_roles" not in profile and "skills" not in profile)
    check("Frontend lane has must_have skills",
          len(lanes.get("frontend_developer", {}).get("skills", {}).get("must_have", [])) > 0)
    check("Marketing lane has must_have_any gate",
          len(lanes.get("marketing_manager", {}).get("skills", {}).get("must_have_any", [])) > 0)
    check("Has maintenance.stale_listing_max_age_days",
          isinstance(profile.get("maintenance", {}).get("stale_listing_max_age_days"), int))
    check("Has preferences", "preferences" in profile)
    check("Has blacklist", "blacklist" in profile)
    check("salary_min is a number", isinstance(profile["preferences"]["salary_min"], (int, float)))

    # Boards YAML
    boards_path = config_dir / "boards.yaml"
    check("boards.yaml exists", boards_path.exists())

    with open(boards_path) as f:
        boards = yaml.safe_load(f)

    check("Has boards section", "boards" in boards)
    check("Indeed board configured", "indeed" in boards["boards"])

    indeed = boards["boards"]["indeed"]
    check("Indeed has module path", "module" in indeed, indeed.get("module", ""))
    check("Indeed builds queries per lane (no explicit search_queries)",
          not indeed.get("search_queries"))
    check("USAJobs keeps explicit search_queries",
          len(boards["boards"]["usajobs"].get("search_queries", [])) > 0)
    check("Indeed has delay settings", "delay_min" in indeed and "delay_max" in indeed)
    check("delay_min < delay_max", indeed["delay_min"] < indeed["delay_max"],
          f"{indeed['delay_min']} < {indeed['delay_max']}")

    # Secrets env
    secrets_path = config_dir / "secrets.env"
    check("secrets.env exists", secrets_path.exists())


# =====================================================================
# Salary parser tests
# =====================================================================

def test_salary_parser() -> None:
    print("\n=== Salary Parser ===")

    # Import the static method logic directly (no deps needed)
    def parse_salary_text(text: str) -> tuple:
        if not text:
            return None, None
        amounts = re.findall(r"\$[\d,]+(?:\.\d{2})?", text)
        if not amounts:
            return None, None

        def parse_amount(s):
            return float(s.replace("$", "").replace(",", ""))

        values = [parse_amount(a) for a in amounts]
        is_hourly = "hour" in text.lower() or "hr" in text.lower()
        if is_hourly:
            values = [v * 2080 for v in values]

        if len(values) >= 2:
            return min(values), max(values)
        elif "from" in text.lower() or "at least" in text.lower():
            return values[0], None
        elif "up to" in text.lower():
            return None, values[0]
        else:
            return values[0], values[0]

    cases = [
        ("$80,000 - $100,000 a year", (80000.0, 100000.0)),
        ("$40 - $50 an hour", (83200.0, 104000.0)),
        ("From $90,000 a year", (90000.0, None)),
        ("Up to $120,000 a year", (None, 120000.0)),
        ("$95,000 a year", (95000.0, 95000.0)),
        ("$25.50 - $35.00 an hour", (53040.0, 72800.0)),
        ("Competitive salary", (None, None)),
        ("", (None, None)),
    ]

    for text, expected in cases:
        result = parse_salary_text(text)
        label = text if text else "(empty)"
        check(f"Salary: {label}", result == expected,
              f"got {result}, expected {expected}")


# =====================================================================
# Blacklist logic tests
# =====================================================================

def test_blacklist() -> None:
    print("\n=== Blacklist Logic ===")

    config_dir = Path(__file__).parent.parent / "config"
    with open(config_dir / "profile.yaml") as f:
        profile = yaml.safe_load(f)

    blacklist_companies = [c.lower() for c in profile.get("blacklist", {}).get("companies", [])]
    blacklist_keywords = [k.lower() for k in profile.get("blacklist", {}).get("keywords", [])]

    def is_blacklisted(title, company, description=""):
        if company:
            for blocked in blacklist_companies:
                if blocked in company.lower():
                    return True
        text = f"{title} {description}".lower()
        for kw in blacklist_keywords:
            if kw in text:
                return True
        return False

    check("Clean job passes", not is_blacklisted("DevOps Engineer", "Good Corp", "AWS and Linux"))
    check("Blacklisted company caught", is_blacklisted("Engineer", "Company You Hate Inc.", "Great role"))
    check("Blacklisted keyword 'unpaid'", is_blacklisted("Unpaid Internship", "StartupCo"))
    check("Blacklisted keyword 'intern'", is_blacklisted("Software Intern", "BigCo"))
    check("Blacklisted keyword 'clearance required'",
          is_blacklisted("Sys Admin", "GovCo", "US clearance required for this role"))
    check("Keyword in description caught",
          is_blacklisted("Engineer", "NiceCo", "This is an unpaid training program"))


# =====================================================================
# HTML parsing smoke test
# =====================================================================

def test_html_parsing() -> None:
    print("\n=== Indeed HTML Parsing (smoke test) ===")

    # Simulate a minimal Indeed job card
    sample_card = textwrap.dedent("""\
    <div class="job_seen_beacon" data-jk="test789">
      <h2 class="jobTitle"><a href="/viewjob?jk=test789" data-jk="test789">
        <span>Senior Cloud Engineer</span>
      </a></h2>
      <span data-testid="company-name">CloudCorp</span>
      <div data-testid="text-location">Remote</div>
      <div class="salary-snippet-container">$110,000 - $140,000 a year</div>
      <div class="job-snippet">Looking for an experienced cloud engineer with AWS...</div>
      <span class="date">3 days ago</span>
    </div>
    """)

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(sample_card, "lxml")

        card = soup.select_one("div.job_seen_beacon")
        check("Found job card", card is not None)

        title_el = card.select_one("h2.jobTitle a")
        check("Found title", title_el is not None and "Cloud Engineer" in title_el.get_text())

        company_el = card.select_one("[data-testid='company-name']")
        check("Found company", company_el is not None and company_el.get_text(strip=True) == "CloudCorp")

        location_el = card.select_one("[data-testid='text-location']")
        check("Found location", location_el is not None and location_el.get_text(strip=True) == "Remote")

        jk = card.get("data-jk")
        check("Extracted job key", jk == "test789")

        salary_el = card.select_one("div.salary-snippet-container")
        check("Found salary element", salary_el is not None and "$110,000" in salary_el.get_text())

        snippet_el = card.select_one("div.job-snippet")
        check("Found description snippet", snippet_el is not None and "AWS" in snippet_el.get_text())

    except ImportError:
        print("  [SKIP] beautifulsoup4/lxml not installed — skipping HTML tests")
        print("         Install with: pip install beautifulsoup4 lxml")


# =====================================================================
# Search lane config tests (uses app.config)
# =====================================================================

LANES_YAML = textwrap.dedent("""\
    search_lanes:
      frontend_developer:
        enabled: true
        target_roles: ["Lead Frontend Developer"]
        target_field: "software/web development"
        skills:
          must_have: ["JavaScript", "React"]
          nice_to_have: ["Node.js"]
      marketing_manager:
        enabled: {marketing_enabled}
        target_roles: ["Marketing Manager"]
        target_field: "digital marketing"
        skills:
          must_have_any: ["AEM", "HTML"]
          nice_to_have: ["Marketo"]
    preferences:
      location: "Homewood, IL"
      salary_min: 130000
      experience_years: 15
    maintenance:
      stale_listing_max_age_days: 45
""")


def _write_profile(tmpdir: str, marketing_enabled: bool) -> Path:
    path = Path(tmpdir) / f"profile_{marketing_enabled}.yaml"
    path.write_text(LANES_YAML.format(marketing_enabled=str(marketing_enabled).lower()))
    return path


def test_search_lanes_config() -> None:
    print("\n=== Search Lane Config ===")
    from app.config import load_profile, load_settings

    with tempfile.TemporaryDirectory() as tmpdir:
        profile = load_profile(_write_profile(tmpdir, marketing_enabled=True))
        check("Two lanes loaded", len(profile.search_lanes) == 2, str(list(profile.search_lanes)))
        check("Lane name injected from key",
              profile.search_lanes["marketing_manager"].name == "marketing_manager")
        check("Both lanes enabled",
              [l.name for l in profile.enabled_lanes] == ["frontend_developer", "marketing_manager"])
        check("must_have_any parsed",
              profile.search_lanes["marketing_manager"].skills.must_have_any == ["AEM", "HTML"])
        check("must_have_any defaults empty",
              profile.search_lanes["frontend_developer"].skills.must_have_any == [])
        check("Maintenance config parsed", profile.maintenance.stale_listing_max_age_days == 45)
        check("preserve_statuses default",
              profile.maintenance.preserve_statuses == ["approved", "applied"])

        profile = load_profile(_write_profile(tmpdir, marketing_enabled=False))
        check("enabled_lanes respects enabled=false",
              [l.name for l in profile.enabled_lanes] == ["frontend_developer"])

    # The real config must load through the models too
    settings = load_settings()
    check("Real profile loads with lanes", len(settings.profile.enabled_lanes) >= 2)
    indeed = settings.boards.boards.get("indeed")
    check("Board without queries gets no profile default",
          indeed is not None and indeed.search_queries == [])


# =====================================================================
# Lane-aware DB operations (uses app.database)
# =====================================================================

async def _lane_db_checks(db_path: Path) -> None:
    from app.database import Database, EvaluationRecord, JobRecord

    async with Database(db_path) as db:
        url = "https://example.com/job/1"
        job_id = await db.insert_job(JobRecord(
            source="indeed", url=url, title="Web Marketing Manager",
            search_lane="frontend_developer",
        ))
        job = await db.get_job(job_id)
        check("insert_job stores search_lane", job.search_lane == "frontend_developer")

        promoted = await db.add_lane_to_job(url, "frontend_developer")
        job = await db.get_job(job_id)
        check("Same lane is a no-op", not promoted and job.search_lane == "frontend_developer")

        promoted = await db.add_lane_to_job(url, "marketing_manager")
        job = await db.get_job(job_id)
        check("Different lane promotes to 'both'", promoted and job.search_lane == "both")

        promoted = await db.add_lane_to_job(url, "marketing_manager")
        promoted_again = await db.add_lane_to_job(url, "frontend_developer")
        job = await db.get_job(job_id)
        check("add_lane_to_job is idempotent on 'both'",
              not promoted and not promoted_again and job.search_lane == "both")

        # Per-lane evaluations
        await db.insert_evaluation(EvaluationRecord(
            job_id=job_id, model_used="m", match_score=0.3, search_lane="frontend_developer"))
        await db.insert_evaluation(EvaluationRecord(
            job_id=job_id, model_used="m", match_score=0.8, search_lane="marketing_manager"))
        fe = await db.get_evaluation(job_id, lane="frontend_developer")
        mm = await db.get_evaluation(job_id, lane="marketing_manager")
        check("get_evaluation filters by lane",
              fe.match_score == 0.3 and mm.match_score == 0.8)

        rows, total = await db.get_jobs_paginated(lane="marketing_manager")
        check("'both' job appears in lane filter", total == 1 and rows[0]["id"] == job_id)
        check("Joined eval is the best lane score, one row per job",
              len(rows) == 1 and rows[0]["eval_score"] == 0.8
              and rows[0]["eval_lane"] == "marketing_manager")

        stats = await db.get_stats()
        check("get_stats has by_lane", stats.get("by_lane") == {"both": 1}, str(stats))
        check("get_stats total unaffected by by_lane", stats.get("total") == 1)


async def _purge_checks(db_path: Path) -> None:
    from app.database import Database, DecisionRecord, EvaluationRecord, JobRecord

    async with Database(db_path) as db:
        ids = {}
        for title in ("new", "rejected", "approved", "applied", "fresh"):
            ids[title] = await db.insert_job(JobRecord(
                source="indeed", url=f"https://example.com/{title}", title=title,
                status="new" if title == "fresh" else title,
                search_lane="frontend_developer",
            ))
            await db.insert_evaluation(EvaluationRecord(
                job_id=ids[title], model_used="m", match_score=0.5,
                search_lane="frontend_developer"))

        await db.insert_decision(DecisionRecord(job_id=ids["rejected"], decision="rejected"))

        # Age everything except "fresh" past the cutoff
        await db.conn.execute(
            "UPDATE jobs SET date_scraped = datetime('now', '-40 days') WHERE id != ?",
            (ids["fresh"],),
        )
        await db.conn.commit()

        deleted = await db.purge_stale_listings(max_age_days=30)
        check("Purge deleted old new + rejected jobs", deleted == 2, f"deleted={deleted}")

        cursor = await db.conn.execute("SELECT title FROM jobs")
        remaining = {r[0] for r in await cursor.fetchall()}
        check("Purge preserved approved/applied/fresh",
              remaining == {"approved", "applied", "fresh"}, str(remaining))

        cursor = await db.conn.execute(
            "SELECT COUNT(*) FROM evaluations WHERE job_id NOT IN (SELECT id FROM jobs)"
        )
        check("Purge removed related evaluations", (await cursor.fetchone())[0] == 0)

        cursor = await db.conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE job_id NOT IN (SELECT id FROM jobs)"
        )
        check("Purge removed related decisions", (await cursor.fetchone())[0] == 0)

        check("Purge with nothing stale returns 0",
              await db.purge_stale_listings(max_age_days=30) == 0)


LEGACY_SCHEMA = """
CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
    external_id TEXT, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, company TEXT,
    location TEXT, salary_min REAL, salary_max REAL, description TEXT, raw_html TEXT,
    date_posted TEXT, date_scraped TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'new');
CREATE TABLE evaluations (id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id), model_used TEXT NOT NULL,
    match_score REAL, reasoning TEXT, cover_letter_draft TEXT,
    evaluated_at TEXT DEFAULT (datetime('now')));
INSERT INTO jobs (source, url, title) VALUES ('indeed', 'https://old/1', 'Old Job');
"""


async def _migration_checks(db_path: Path) -> None:
    """A pre-lanes DB (no search_lane columns) should migrate cleanly."""
    from app.database import Database

    conn = sqlite3.connect(db_path)
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    conn.close()

    async with Database(db_path) as db:
        job = await db.get_job(1)
        check("Legacy job migrated to frontend_developer lane",
              job.search_lane == "frontend_developer")
        cursor = await db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_jobs_lane'"
        )
        check("idx_jobs_lane created after migration", await cursor.fetchone() is not None)


def test_lane_database() -> None:
    print("\n=== Lane-aware Database ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        asyncio.run(_lane_db_checks(Path(tmpdir) / "lanes.db"))
        asyncio.run(_purge_checks(Path(tmpdir) / "purge.db"))
        asyncio.run(_migration_checks(Path(tmpdir) / "legacy.db"))


# =====================================================================
# Eval prompt per lane (uses app.evaluator.pipeline)
# =====================================================================

def test_eval_prompt_lanes() -> None:
    print("\n=== Eval Prompt per Lane ===")
    from app.config import load_profile
    from app.database import JobRecord
    from app.evaluator.pipeline import build_eval_prompt

    with tempfile.TemporaryDirectory() as tmpdir:
        profile = load_profile(_write_profile(tmpdir, marketing_enabled=True))

    job = JobRecord(source="indeed", url="https://x", title="Marketing Manager",
                    description="Own our AEM site")
    gate = "Gate skills (job must list at least ONE, or cap at 0.35): "

    mm_prompt = build_eval_prompt(profile, profile.search_lanes["marketing_manager"], job)
    check("Marketing prompt renders gate skills", gate + "AEM, HTML" in mm_prompt)
    check("Marketing prompt uses lane target_field",
          "Target field: digital marketing" in mm_prompt)
    check("Marketing prompt uses lane target roles",
          "Target Roles: Marketing Manager" in mm_prompt)

    fe_prompt = build_eval_prompt(profile, profile.search_lanes["frontend_developer"], job)
    check("Frontend prompt renders 'None (no gate)'", gate + "None (no gate)" in fe_prompt)
    check("Frontend prompt uses lane core skills", "Core skills: JavaScript, React" in fe_prompt)


# =====================================================================
# Run all tests
# =====================================================================

if __name__ == "__main__":
    print("Job Agent — Phase 1 Core Tests")
    print("=" * 50)

    test_database()
    test_config()
    test_salary_parser()
    test_blacklist()
    test_html_parsing()
    test_search_lanes_config()
    test_lane_database()
    test_eval_prompt_lanes()

    print("\n" + "=" * 50)
    print(f"Results: {PASS} passed, {FAIL} failed")

    if FAIL > 0:
        sys.exit(1)
    else:
        print("All tests passed!")
        sys.exit(0)
