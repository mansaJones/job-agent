"""Tests for the lane-aware cover letter pipeline, rendering, and legacy migration.

Run: pytest tests/test_cover_letter.py
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import (
    AppSettings, BoardsConfig, ContactConfig, ProfileConfig, SearchLaneConfig, SecretsConfig,
)
from app.database import Database, EvaluationRecord, JobRecord
from app.polisher.claude_client import PolishResult
from app.polisher.pipeline import (
    DOC_TYPE, MANUAL_EDIT_MODEL, PolishPipeline, _pick_template, cover_letter_paths,
)
from app.resume_generator.formatter import render_cover_letter_docx, render_cover_letter_pdf
from app.resume_generator.models import (
    LinkedInData, Position, ResumeBullet, ResumePosition, TailoredResume,
)

LANES = {
    "frontend_developer": SearchLaneConfig(
        name="frontend_developer", target_roles=["Lead Frontend Developer"],
        target_field="web development", resume_version="frontend_developer"),
    "marketing_manager": SearchLaneConfig(
        name="marketing_manager", target_roles=["Marketing Manager"],
        target_field="digital marketing", resume_version="marketing_manager"),
}

LETTER = ("Dear Hiring Manager,\n\nI led the React migration at Acme Corp.\n\n"
          "Sincerely,\nAda Lovelace")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(
        profile=ProfileConfig(
            search_lanes=LANES,
            contact=ContactConfig(email="ada@example.com", phone="555-0100"),
        ),
        boards=BoardsConfig(), secrets=SecretsConfig(),
    )


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    p = {
        "output_dir": tmp_path / "cover_letters",
        "resumes_output_dir": tmp_path / "resumes_out",
        "linkedin_path": tmp_path / "linkedin_data.json",
        "bullets_path": tmp_path / "no_bullets.json",
        "static_resumes_dir": tmp_path / "static",
    }
    p["resumes_output_dir"].mkdir()
    p["static_resumes_dir"].mkdir()
    return p


def _write_linkedin(path: Path) -> None:
    path.write_text(LinkedInData(
        first_name="Ada", last_name="Lovelace", headline="Frontend Lead",
        positions=[Position(company="Acme Corp", title="Lead Developer", start_date="2022-04",
                            bullets=["Led React migration"])],
        education=[], skills=["React"], certifications=[], exported_at="2026-09-29T00:00:00",
    ).model_dump_json(), encoding="utf-8")


def _write_tailored(resumes_dir: Path, job_id: int, lane: str) -> Path:
    path = resumes_dir / f"{job_id}_{lane}_resume.json"
    path.write_text(TailoredResume(
        full_name="Ada Lovelace", headline="Tailored headline", contact_line="c",
        summary="Tailored summary.",
        positions=[ResumePosition(company="Acme Corp", title="Lead Developer",
                                  start_date="2022-04",
                                  bullets=[ResumeBullet(text="Shipped the thing")])],
        skills=["React"],
    ).model_dump_json(), encoding="utf-8")
    return path


def _mock_claude(text: str = LETTER) -> AsyncMock:
    claude = AsyncMock()
    claude.generate_cover_letter.return_value = PolishResult(
        cover_letter=text, model_used="test-model", input_tokens=10, output_tokens=5,
        cost_estimate=0.001,
    )
    return claude


async def _job(db: Database, lane: str = "frontend_developer", **kw) -> int:
    return await db.insert_job(JobRecord(
        source="indeed", url=f"https://x/{lane}/{kw.get('title', 'job')}",
        title=kw.get("title", "Lead Frontend Developer"), company="Acme",
        description="We need a React lead to own our frontend platform.", search_lane=lane,
    ))


# ---------------------------------------------------------------------------
# Template selection
# ---------------------------------------------------------------------------

def test_pick_template() -> None:
    assert "Marketing" in _pick_template("marketing_manager", "Marketing Manager")
    assert "Leadership" in _pick_template("frontend_developer", "Lead Frontend Developer")
    assert "Individual Contributor" in _pick_template("frontend_developer",
                                                      "Senior Frontend Developer")
    assert "General" in _pick_template("data_science", "Data Scientist")


def test_pick_template_missing_file(monkeypatch, tmp_path: Path) -> None:
    import app.polisher.pipeline as pl

    monkeypatch.setattr(pl, "TEMPLATES_DIR", tmp_path)
    assert _pick_template("marketing_manager", "Marketing Manager") is None


# ---------------------------------------------------------------------------
# Candidate context priority
# ---------------------------------------------------------------------------

async def test_resolve_candidate_context_priority(settings, paths, tmp_path: Path) -> None:
    lane = LANES["frontend_developer"]
    async with Database(tmp_path / "jobs.db") as db:
        job_id = await _job(db)
        pipeline = PolishPipeline(db, None, settings, **paths)

        tailored = _write_tailored(paths["resumes_output_dir"], job_id, lane.name)
        _write_linkedin(paths["linkedin_path"])
        static_pdf = paths["static_resumes_dir"] / "jj Frontend_Developer resume.pdf"
        render_cover_letter_pdf("Static resume body text.", "Ada Lovelace",
                                ContactConfig(), static_pdf)
        # A PDF for another lane must not be picked
        render_cover_letter_pdf("Wrong lane.", "Ada", ContactConfig(),
                                paths["static_resumes_dir"] / "marketing_manager.pdf")

        text, source = await pipeline._resolve_candidate_context(job_id, lane)
        assert source == "tailored_resume" and "Tailored headline" in text

        tailored.unlink()
        text, source = await pipeline._resolve_candidate_context(job_id, lane)
        assert source == "linkedin_data" and "Led React migration" in text

        paths["linkedin_path"].unlink()
        text, source = await pipeline._resolve_candidate_context(job_id, lane)
        assert source == "static_pdf" and "Static resume body text." in text

        static_pdf.unlink()
        with pytest.raises(FileNotFoundError, match="parse-linkedin"):
            await pipeline._resolve_candidate_context(job_id, lane)


# ---------------------------------------------------------------------------
# Generation, cache, edits
# ---------------------------------------------------------------------------

async def test_polish_job_cache_and_force(settings, paths, tmp_path: Path) -> None:
    _write_linkedin(paths["linkedin_path"])
    claude = _mock_claude()
    async with Database(tmp_path / "jobs.db") as db:
        job_id = await _job(db)
        await db.insert_evaluation(EvaluationRecord(
            job_id=job_id, model_used="m", match_score=0.8, reasoning="Strong React fit",
            search_lane="frontend_developer"))
        pipeline = PolishPipeline(db, claude, settings, **paths)

        first = await pipeline.polish_job(job_id, "frontend_developer")
        assert not first.from_cache and first.context_source == "linkedin_data"
        assert first.txt_path.read_text(encoding="utf-8") == LETTER
        assert first.pdf_path.exists() and first.docx_path.exists()

        kwargs = claude.generate_cover_letter.await_args.kwargs
        assert kwargs["full_name"] == "Ada Lovelace"
        assert kwargs["contact"].email == "ada@example.com"
        assert kwargs["lane_target_field"] == "web development"
        assert kwargs["eval_highlights"] == ["Strong React fit"]
        assert "Leadership" in kwargs["template"]

        second = await pipeline.polish_job(job_id, "frontend_developer")
        assert second.from_cache and second.text == LETTER
        assert claude.generate_cover_letter.await_count == 1

        await pipeline.polish_job(job_id, "frontend_developer", force=True)
        assert claude.generate_cover_letter.await_count == 2

        # Legacy column is no longer written
        cursor = await db.conn.execute(
            "SELECT COUNT(*) FROM evaluations WHERE cover_letter_draft IS NOT NULL")
        assert (await cursor.fetchone())[0] == 0

        # A tailored resume changes the context → cache miss
        _write_tailored(paths["resumes_output_dir"], job_id, "frontend_developer")
        aligned = await pipeline.polish_job(job_id, "frontend_developer")
        assert not aligned.from_cache and aligned.context_source == "tailored_resume"
        assert claude.generate_cover_letter.await_count == 3


async def test_saved_edit_is_not_overwritten(settings, paths, tmp_path: Path) -> None:
    _write_linkedin(paths["linkedin_path"])
    claude = _mock_claude()
    async with Database(tmp_path / "jobs.db") as db:
        job_id = await _job(db)
        pipeline = PolishPipeline(db, claude, settings, **paths)
        await pipeline.polish_job(job_id, "frontend_developer")

        edited_text = "Dear Team,\n\nMy own words.\n\nBest,\nAda Lovelace"
        edited = await pipeline.save_edited_cover_letter(
            job_id, "frontend_developer", edited_text.replace("\n", "\r\n"))
        assert edited.edited
        doc = await db.get_generated_document(job_id, "frontend_developer", DOC_TYPE)
        assert doc["model_used"] == MANUAL_EDIT_MODEL
        assert doc["content_hash"].startswith("edited-")
        assert doc["updated_at"] is not None

        again = await pipeline.polish_job(job_id, "frontend_developer")
        assert again.from_cache and again.text == edited_text
        assert claude.generate_cover_letter.await_count == 1

        forced = await pipeline.polish_job(job_id, "frontend_developer", force=True)
        assert forced.text == LETTER and not forced.edited


async def test_lane_mismatch_rejected(settings, paths, tmp_path: Path) -> None:
    async with Database(tmp_path / "jobs.db") as db:
        job_id = await _job(db, lane="marketing_manager", title="Marketing Manager")
        pipeline = PolishPipeline(db, _mock_claude(), settings, **paths)
        with pytest.raises(ValueError, match="lane"):
            await pipeline.polish_job(job_id, "frontend_developer")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_render_cover_letter(tmp_path: Path) -> None:
    import docx
    import pymupdf

    contact = ContactConfig(email="ada@example.com", phone="555-0100")
    pdf = render_cover_letter_pdf(LETTER, "Ada Lovelace", contact, tmp_path / "cl.pdf")
    with pymupdf.open(pdf) as doc:
        text = "".join(page.get_text() for page in doc)
    assert "Ada Lovelace" in text
    assert "I led the React migration at Acme Corp." in text
    assert "ada@example.com" in text

    out = render_cover_letter_docx(LETTER, "Ada Lovelace", contact, tmp_path / "cl.docx")
    paragraphs = [p.text for p in docx.Document(str(out)).paragraphs]
    assert paragraphs[0] == "Ada Lovelace"
    assert "I led the React migration at Acme Corp." in paragraphs


# ---------------------------------------------------------------------------
# Legacy migration
# ---------------------------------------------------------------------------

async def test_migrate_legacy_is_idempotent(settings, paths, tmp_path: Path) -> None:
    async with Database(tmp_path / "jobs.db") as db:
        job_id = await _job(db, lane="both")
        await db.insert_evaluation(EvaluationRecord(
            job_id=job_id, model_used="m", match_score=0.7,
            cover_letter_draft="Old draft.\n\nThanks,\nAda"))
        pipeline = PolishPipeline(db, None, settings, **paths)

        assert await pipeline.migrate_legacy_cover_letters() == 1
        assert await pipeline.migrate_legacy_cover_letters() == 0

        cursor = await db.conn.execute("SELECT search_lane, model_used FROM generated_documents")
        rows = [tuple(r) for r in await cursor.fetchall()]
        assert rows == [("frontend_developer", "legacy-migration")]  # 'both' → frontend

        txt, pdf, docx_path = cover_letter_paths(paths["output_dir"], job_id,
                                                 "frontend_developer")
        assert txt.read_text(encoding="utf-8") == "Old draft.\n\nThanks,\nAda"
        assert pdf.exists() and docx_path.exists()

        # Migrated drafts aren't silently replaced by a non-forced generate
        _write_linkedin(paths["linkedin_path"])
        pipeline.claude = _mock_claude()
        kept = await pipeline.polish_job(job_id, "frontend_developer")
        assert kept.from_cache and kept.context_source == "legacy"
        pipeline.claude.generate_cover_letter.assert_not_awaited()
