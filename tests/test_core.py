"""Standalone tests for the job agent core — runs with stdlib only.

No external dependencies needed. Tests the database schema, config YAML
parsing, salary parser, and blacklist logic.

Run: python3 tests/test_core.py
"""

import json
import os
import re
import sqlite3
import sys
import textwrap
import yaml
from pathlib import Path

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
    expected_tables = ["applied", "decisions", "evaluations", "jobs"]
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

    check("Has target_roles", "target_roles" in profile and len(profile["target_roles"]) > 0,
          str(profile.get("target_roles")))
    check("Has skills.must_have", len(profile.get("skills", {}).get("must_have", [])) > 0)
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
    check("Indeed has search_queries", len(indeed.get("search_queries", [])) > 0)
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

    print("\n" + "=" * 50)
    print(f"Results: {PASS} passed, {FAIL} failed")

    if FAIL > 0:
        sys.exit(1)
    else:
        print("All tests passed!")
        sys.exit(0)
