"""End-to-end runner flow against a fake Jetson and local fixture pages (headless)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console

from apply_client.config import ClientConfig
from apply_client.models import ApplyRequest, JobInfo
from apply_client.runner import ApplyRunner
from conftest import FIXTURES


class FakeJetson:
    def __init__(self) -> None:
        self.progress_calls: list[dict] = []
        self.results: list[tuple[str, str | None]] = []

    async def download_document(self, queue_id, doc_type, fmt, dest_dir: Path) -> Path:  # type: ignore[no-untyped-def]
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"Lovelace_{doc_type}.{fmt}"
        path.write_bytes(b"%PDF-1.4")
        return path

    async def progress(self, queue_id, **fields) -> None:  # type: ignore[no-untyped-def]
        self.progress_calls.append(fields)

    async def result(self, queue_id, status, notes=None) -> None:  # type: ignore[no-untyped-def]
        self.results.append((status, notes))


@pytest.fixture
async def context():
    playwright_api = pytest.importorskip("playwright.async_api")
    async with playwright_api.async_playwright() as pw:
        browser = None
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                browser = await pw.chromium.launch(headless=True, **kwargs)
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("No Chromium/Chrome available")
        ctx = await browser.new_context()
        yield ctx
        await browser.close()


def _cfg(tmp_path: Path, timeout_minutes: float = 1.0) -> ClientConfig:
    return ClientConfig(jetson_url="http://jetson.test", apply_client_token="t",
                        chrome_profile_dir=tmp_path / "profile", download_dir=tmp_path / "dl",
                        human_timeout_minutes=timeout_minutes)


def _request(applicant, url: str, source: str = "company") -> ApplyRequest:  # type: ignore[no-untyped-def]
    return ApplyRequest(
        queue_id=7, job_id=42, lane="frontend_developer", status="claimed",
        job=JobInfo(title="Lead Frontend Developer", company="Acme", url=url, source=source),
        applicant=applicant,
        documents={"resume": {"pdf": "/r"}, "cover_letter": {"pdf": "/c"}},
    )


async def _wait_for(predicate, timeout: float = 20.0) -> None:  # type: ignore[no-untyped-def]
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.1)


async def test_full_flow_fills_and_waits_for_done(context, applicant, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    api = FakeJetson()
    runner = ApplyRunner(_cfg(tmp_path), api, context, Console(quiet=True))
    listing = (FIXTURES / "listing.html").as_uri()
    task = asyncio.create_task(runner.process(_request(applicant, listing)))

    # Wait for the fill pass to report counts
    await _wait_for(lambda: any(c.get("fields_filled") for c in api.progress_calls))
    page = context.pages[-1]
    assert page.url.endswith("greenhouse_form.html")        # clicked through the listing
    assert await page.evaluate("document.getElementById('first_name').value") == "Ada"
    assert await page.evaluate("!!window.__submitted") is False

    last = [c for c in api.progress_calls if c.get("fields_filled")][-1]
    assert last["status"] == "in_progress" and last["ats_detected"] == "GREENHOUSE"
    assert last["fields_filled"] == 12 and last["fields_flagged"] == 7  # 6 flagged + 1 sensitive

    # Overlay: real Done button, two clicks (the first only arms it)
    done = page.locator("#job-agent-overlay").locator("#done")
    await done.click()
    await asyncio.sleep(0.3)
    assert not task.done()
    await done.click()
    await asyncio.wait_for(task, timeout=10)

    assert api.results[-1][0] == "completed"
    assert "GREENHOUSE: filled 12" in api.results[-1][1]
    assert context.pages == []                                # tab closed
    assert not (tmp_path / "dl" / "7").exists()               # downloads cleaned up


async def test_usajobs_is_handed_off_and_times_out(context, applicant, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    api = FakeJetson()
    runner = ApplyRunner(_cfg(tmp_path, timeout_minutes=0.03), api, context, Console(quiet=True))
    listing = (FIXTURES / "listing.html").as_uri()
    await asyncio.wait_for(runner.process(_request(applicant, listing, source="usajobs")), timeout=20)

    assert "login.gov" in (api.progress_calls[0].get("notes") or "")
    assert not any(c.get("fields_filled") for c in api.progress_calls)  # nothing auto-filled
    status, notes = api.results[-1]
    assert status == "abandoned" and "Timed out" in notes
    assert context.pages == []


async def test_missing_applicant_fails_cleanly(context, applicant, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    api = FakeJetson()
    runner = ApplyRunner(_cfg(tmp_path), api, context, Console(quiet=True))
    request = _request(applicant, "https://example.invalid/job")
    request.applicant, request.applicant_error = None, "Missing applicant fields: linkedin_url"
    await runner.process(request)
    status, notes = api.results[-1]
    assert status == "failed" and "linkedin_url" in notes
