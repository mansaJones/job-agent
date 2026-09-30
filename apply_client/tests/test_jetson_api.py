"""Jetson API client — auth header, retries, never-retried claim, error mapping (no network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import apply_client.jetson_api as jetson_api
from apply_client.jetson_api import AuthError, ConflictError, JetsonAPI, JetsonAPIError

PENDING = [{
    "queue_id": 3, "job_id": 9, "lane": "frontend_developer", "status": "pending",
    "queued_at": "2026-09-30 10:00:00",
    "job": {"title": "Lead", "company": "Acme", "url": "https://x", "source": "indeed",
            "description": "..."},
    "applicant": None, "applicant_error": "Missing applicant fields: linkedin_url",
    "documents": {"resume": {"pdf": "/api/apply-queue/3/documents/resume?fmt=pdf"}},
}]


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(jetson_api, "BACKOFF_SECONDS", (0.0, 0.0, 0.0))


def _api(handler) -> JetsonAPI:  # type: ignore[no-untyped-def]
    return JetsonAPI("http://jetson.test", "secret", transport=httpx.MockTransport(handler))


async def test_token_header_and_pending_parsing() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-Apply-Token"))
        return httpx.Response(200, json=PENDING)

    async with _api(handler) as api:
        pending = await api.get_pending()
    assert seen == ["secret"]
    assert pending[0].queue_id == 3 and pending[0].applicant is None


async def test_connection_errors_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"ok": True, "pending": 0})

    async with _api(handler) as api:
        assert await api.health() == {"ok": True, "pending": 0}
    assert len(calls) == 3


async def test_claim_never_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("down")

    async with _api(handler) as api:
        with pytest.raises(JetsonAPIError):
            await api.claim(3)
    assert len(calls) == 1


@pytest.mark.parametrize("status, error", [(401, AuthError), (409, ConflictError)])
async def test_error_mapping(status: int, error: type) -> None:
    async with _api(lambda r: httpx.Response(status, json={"detail": "nope"})) as api:
        with pytest.raises(error):
            await api.claim(3)


async def test_duplicate_result_is_tolerated() -> None:
    async with _api(lambda r: httpx.Response(409, json={"detail": "not active"})) as api:
        await api.result(3, "completed", "done")  # no exception


async def test_download_uses_server_filename(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fmt"] == "pdf"
        return httpx.Response(200, content=b"%PDF", headers={
            "content-disposition": 'attachment; filename="Jones_Acme_resume.pdf"'})

    async with _api(handler) as api:
        path = await api.download_document(3, "resume", "pdf", tmp_path / "3")
    assert path.name == "Jones_Acme_resume.pdf" and path.read_bytes() == b"%PDF"
