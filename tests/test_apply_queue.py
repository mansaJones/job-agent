"""Tests for the apply queue: DB lifecycle, pre-flight checks, and the token-protected API.

Run: pytest tests/test_apply_queue.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.applicator.preflight import has_blockers, has_warnings, run_preflight
from app.config import (
    AppSettings, BoardsConfig, ContactConfig, ProfileConfig, SearchLaneConfig, SecretsConfig,
)
from app.database import ApplyQueueError, Database, JobRecord
from app.resume_generator.models import LinkedInData, Position, TailoredResume

LANES = {"frontend_developer": SearchLaneConfig(name="frontend_developer")}
TOKEN = "test-token-123"


def _settings(tmp_path: Path, token: str = TOKEN) -> AppSettings:
    return AppSettings(
        profile=ProfileConfig(
            search_lanes=LANES,
            contact=ContactConfig(email="ada@example.com", phone="555-0100",
                                  linkedin_url="https://linkedin.com/in/ada"),
        ),
        boards=BoardsConfig(),
        secrets=SecretsConfig(apply_client_token=token),
        db_path=tmp_path / "data" / "jobs.db",
    )


def _linkedin() -> LinkedInData:
    return LinkedInData(
        first_name="Ada", last_name="Lovelace",
        positions=[Position(company="Acme", title="Lead", start_date="2022-01")],
        education=[], skills=["React"], certifications=[], exported_at="2026-09-29T00:00:00",
    )


async def _job(db: Database, n: int = 1, status: str = "evaluated") -> int:
    return await db.insert_job(JobRecord(
        source="indeed", url=f"https://x/{n}", title=f"Job {n}", company="Acme",
        status=status, search_lane="frontend_developer",
    ))


async def _docs(db: Database, job_id: int, tmp_path: Path,
                fabrication: list[str] | None = None, edited: bool = True) -> tuple[int, int]:
    out = tmp_path / "docs"
    out.mkdir(exist_ok=True)
    resume_pdf = out / f"{job_id}_frontend_developer_resume.pdf"
    resume_pdf.write_bytes(b"%PDF-1.4 resume")
    resume_pdf.with_suffix(".docx").write_bytes(b"docx")
    resume_pdf.with_suffix(".json").write_text(TailoredResume(
        full_name="Ada Lovelace", headline="h", contact_line="c", summary="s",
        positions=[], skills=[], fabrication_warnings=fabrication or [],
    ).model_dump_json(), encoding="utf-8")
    letter_pdf = out / f"{job_id}_frontend_developer_cover_letter.pdf"
    letter_pdf.write_bytes(b"%PDF-1.4 letter")
    r = await db.insert_generated_document(job_id, "frontend_developer", "resume",
                                           str(resume_pdf), "m", "h1")
    c = await db.insert_generated_document(job_id, "frontend_developer", "cover_letter",
                                           str(letter_pdf),
                                           "manual-edit" if edited else "m", "h2")
    return r, c


# ---------------------------------------------------------------------------
# Database lifecycle
# ---------------------------------------------------------------------------

async def test_claim_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    async with Database(path) as db:
        job_id = await _job(db)
        queue_id = await db.enqueue_apply(job_id, "frontend_developer", None, None)

    # Two separate connections race for the same row
    async with Database(path) as a, Database(path) as b:
        results = await asyncio.gather(a.claim_apply_request(queue_id),
                                       b.claim_apply_request(queue_id))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1 and results.count(None) == 1
    assert winners[0]["status"] == "claimed" and winners[0]["started_at"]


async def test_enqueue_refuses_second_active_row(tmp_path: Path) -> None:
    async with Database(tmp_path / "jobs.db") as db:
        job_id = await _job(db)
        await db.enqueue_apply(job_id, "frontend_developer", None, None)
        with pytest.raises(ApplyQueueError):
            await db.enqueue_apply(job_id, "frontend_developer", None, None)

        # Once finished, the job can be queued again
        queue_id = (await db.get_active_apply_request(job_id))["id"]
        await db.complete_apply_request(queue_id, "abandoned", "walked away")
        assert await db.enqueue_apply(job_id, "frontend_developer", None, None)


async def test_complete_records_application(tmp_path: Path) -> None:
    async with Database(tmp_path / "jobs.db") as db:
        job_id = await _job(db)
        queue_id = await db.enqueue_apply(job_id, "frontend_developer", None, None)
        await db.claim_apply_request(queue_id)
        await db.update_apply_progress(queue_id, "in_progress", ats_detected="GREENHOUSE",
                                       apply_url="https://boards.greenhouse.io/x",
                                       fields_filled=8, fields_flagged=2)
        await db.complete_apply_request(queue_id, "completed", "Submitted")

        row = await db.get_apply_request(queue_id)
        assert row["status"] == "completed" and row["completed_at"]
        assert (row["ats_detected"], row["fields_filled"]) == ("GREENHOUSE", 8)
        assert (await db.get_job(job_id)).status == "applied"
        cursor = await db.conn.execute("SELECT method FROM applied WHERE job_id = ?", (job_id,))
        assert [r[0] for r in await cursor.fetchall()] == ["assisted"]

        # A retried result call must not record a second application
        with pytest.raises(ApplyQueueError):
            await db.complete_apply_request(queue_id, "completed", "again")


async def test_progress_requires_claim(tmp_path: Path) -> None:
    async with Database(tmp_path / "jobs.db") as db:
        queue_id = await db.enqueue_apply(await _job(db), "frontend_developer", None, None)
        with pytest.raises(ApplyQueueError):
            await db.update_apply_progress(queue_id, "in_progress")  # still pending
        await db.claim_apply_request(queue_id)
        with pytest.raises(ApplyQueueError):
            await db.update_apply_progress(queue_id, "completed")  # terminal → use result


async def test_reset_stale_claims_threshold(tmp_path: Path) -> None:
    async with Database(tmp_path / "jobs.db") as db:
        stale = await db.enqueue_apply(await _job(db, 1), "frontend_developer", None, None)
        fresh = await db.enqueue_apply(await _job(db, 2), "frontend_developer", None, None)
        pending = await db.enqueue_apply(await _job(db, 3), "frontend_developer", None, None)
        await db.claim_apply_request(stale)
        await db.claim_apply_request(fresh)
        await db.conn.execute(
            "UPDATE apply_queue SET status = 'in_progress', "
            "started_at = datetime('now', '-45 minutes') WHERE id = ?", (stale,))
        await db.conn.execute(
            "UPDATE apply_queue SET started_at = datetime('now', '-10 minutes') WHERE id = ?",
            (fresh,))
        await db.conn.commit()

        assert await db.reset_stale_claims(max_age_minutes=30) == 1
        assert (await db.get_apply_request(stale))["status"] == "pending"
        assert (await db.get_apply_request(fresh))["status"] == "claimed"
        assert (await db.get_apply_request(pending))["status"] == "pending"


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

async def test_preflight_missing_resume_blocks(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    async with Database(tmp_path / "jobs.db") as db:
        job_id = await _job(db)
        docs = await db.get_generated_documents_for_job(job_id)
        checks = run_preflight((await db.get_job(job_id)).model_dump(), "frontend_developer",
                               docs, settings, _linkedin())
    by_name = {c.name: c for c in checks}
    assert by_name["Tailored resume"].blocking
    assert "Frontend Developer lane" in by_name["Tailored resume"].detail
    assert by_name["Cover letter"].blocking
    assert has_blockers(checks)


async def test_preflight_fabrication_is_warning_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    async with Database(tmp_path / "jobs.db") as db:
        job_id = await _job(db)
        await _docs(db, job_id, tmp_path, fabrication=["Skill removed: Kubernetes"])
        docs = await db.get_generated_documents_for_job(job_id)
        checks = run_preflight((await db.get_job(job_id)).model_dump(), "frontend_developer",
                               docs, settings, _linkedin())
    fact = next(c for c in checks if c.name == "Resume fact-check")
    assert fact.warning and not fact.blocking and "Kubernetes" in fact.detail
    assert not has_blockers(checks) and has_warnings(checks)


async def test_preflight_blocks_missing_token_and_contact(tmp_path: Path) -> None:
    settings = _settings(tmp_path, token="")
    settings.profile.contact.linkedin_url = ""
    async with Database(tmp_path / "jobs.db") as db:
        job_id = await _job(db)
        await _docs(db, job_id, tmp_path)
        docs = await db.get_generated_documents_for_job(job_id)
        checks = run_preflight((await db.get_job(job_id)).model_dump(), "frontend_developer",
                               docs, settings, _linkedin())
    by_name = {c.name: c for c in checks}
    assert by_name["Apply client token"].blocking
    assert by_name["Applicant data"].blocking and "linkedin_url" in by_name["Applicant data"].detail


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.dashboard.main as dash
    import app.resume_generator.linkedin_parser as lp

    settings = _settings(tmp_path)
    linkedin_path = tmp_path / "linkedin_data.json"
    linkedin_path.write_text(_linkedin().model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(dash, "load_settings", lambda: settings)
    monkeypatch.setattr(lp, "DEFAULT_DATA_PATH", linkedin_path)
    monkeypatch.setattr(lp.load_linkedin_data, "__defaults__", (linkedin_path,))

    with TestClient(dash.app) as client:
        yield client, settings, dash


def test_api_token_auth(api) -> None:
    client, settings, _ = api
    assert client.get("/api/apply-queue/pending").status_code == 401
    assert client.get("/api/apply-queue/pending",
                      headers={"X-Apply-Token": "wrong"}).status_code == 401
    r = client.get("/api/apply-queue/pending", headers={"X-Apply-Token": TOKEN})
    assert r.status_code == 200 and r.json() == []

    settings.secrets.apply_client_token = ""
    r = client.get("/api/apply-queue/pending", headers={"X-Apply-Token": TOKEN})
    assert r.status_code == 503 and "not configured" in r.json()["detail"]


def test_api_full_client_lifecycle(api, tmp_path: Path) -> None:
    client, _, dash = api
    auth = {"X-Apply-Token": TOKEN}
    db = dash.get_db()

    async def seed() -> int:
        job_id = await _job(db)
        await _docs(db, job_id, tmp_path)
        return job_id

    job_id = client.portal.call(seed)

    # Dashboard queues it (all pre-flight checks pass: letter is manual-edit, no fab warnings)
    r = client.post(f"/api/jobs/{job_id}/queue-apply", data={"lane": "frontend_developer"})
    assert r.status_code == 200 and r.json()["queued"], r.json()
    queue_id = r.json()["queue_id"]

    assert client.get("/api/apply-queue/health", headers=auth).json() == {"ok": True, "pending": 1}

    pending = client.get("/api/apply-queue/pending", headers=auth).json()
    assert [p["queue_id"] for p in pending] == [queue_id]
    item = pending[0]
    assert item["applicant"]["email"] == "ada@example.com"
    assert item["applicant"]["state"] == ""  # no preferences.location in the fixture
    assert item["applicant"]["answers"]["sponsorship_needed"] == "No"
    assert set(item["documents"]) == {"resume", "cover_letter"}

    assert client.post(f"/api/apply-queue/{queue_id}/claim", headers=auth).status_code == 200
    assert client.post(f"/api/apply-queue/{queue_id}/claim", headers=auth).status_code == 409

    r = client.get(item["documents"]["resume"]["pdf"], headers=auth)
    assert r.status_code == 200 and r.content == b"%PDF-1.4 resume"
    assert "Lovelace_Acme_resume.pdf" in r.headers["content-disposition"]
    assert client.get(f"/api/apply-queue/{queue_id}/documents/ssn", headers=auth).status_code == 404

    r = client.post(f"/api/apply-queue/{queue_id}/progress", headers=auth,
                    json={"status": "in_progress", "ats_detected": "LEVER", "fields_filled": 5,
                          "fields_flagged": 3})
    assert r.status_code == 200

    r = client.post(f"/api/apply-queue/{queue_id}/result", headers=auth,
                    json={"status": "completed", "notes": "Submitted by human"})
    assert r.status_code == 200
    assert client.post(f"/api/apply-queue/{queue_id}/result", headers=auth,
                       json={"status": "completed"}).status_code == 409

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "applied"

    # Dashboard pages render with the finished row
    page = client.get("/apply-queue")
    assert page.status_code == 200 and "LEVER" in page.text and "every 15s" not in page.text


def test_queue_apply_warnings_need_force(api, tmp_path: Path) -> None:
    client, _, dash = api
    db = dash.get_db()

    async def seed() -> int:
        job_id = await _job(db)
        await _docs(db, job_id, tmp_path, edited=False)  # unreviewed letter → warning
        return job_id

    job_id = client.portal.call(seed)
    hx = {"HX-Request": "true"}
    r = client.post(f"/api/jobs/{job_id}/queue-apply", data={"lane": "frontend_developer"},
                    headers=hx)
    assert "Queue anyway" in r.text and "haven&#39;t reviewed" in r.text

    r = client.post(f"/api/jobs/{job_id}/queue-apply",
                    data={"lane": "frontend_developer", "force": "true"}, headers=hx)
    assert "Queued for the apply client" in r.text

    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200 and "Waiting for the Windows apply client" in detail.text
