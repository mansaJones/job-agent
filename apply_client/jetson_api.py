"""Thin async client for the Jetson's apply queue API (routes under /api/apply-queue/).

Every request carries X-Apply-Token. Connection errors are retried 3x with
backoff — except `claim`, which isn't idempotent and is never retried.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from apply_client.models import ApplyRequest

logger = logging.getLogger(__name__)

RETRIES = 3
BACKOFF_SECONDS = (1.0, 2.0, 4.0)


class JetsonAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthError(JetsonAPIError):
    """401 — APPLY_CLIENT_TOKEN doesn't match the Jetson's apply_client_token."""


class NotConfiguredError(JetsonAPIError):
    """503 — the Jetson has no apply_client_token set yet."""


class ConflictError(JetsonAPIError):
    """409 — already claimed, or the request isn't in a state that allows this."""


class JetsonAPI:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
            headers={"X-Apply-Token": token, "User-Agent": "job-agent-apply-client"},
        )

    async def __aenter__(self) -> JetsonAPI:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, retry: bool = True, **kwargs: Any) -> httpx.Response:
        attempts = RETRIES if retry else 1
        for attempt in range(attempts):
            try:
                response = await self._client.request(method, path, **kwargs)
                break
            except httpx.TransportError as e:
                if attempt == attempts - 1:
                    raise JetsonAPIError(f"Can't reach the Jetson at {self._client.base_url}: {e}") from e
                delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                logger.warning("Jetson unreachable (%s) — retrying in %.0fs", e, delay)
                await asyncio.sleep(delay)

        if response.status_code < 400:
            return response
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        error = {401: AuthError, 503: NotConfiguredError, 409: ConflictError}.get(
            response.status_code, JetsonAPIError)
        raise error(f"{method} {path} → {response.status_code}: {detail}", response.status_code)

    # ---- endpoints ---------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        return (await self._request("GET", "/api/apply-queue/health")).json()

    async def get_pending(self) -> list[ApplyRequest]:
        rows = (await self._request("GET", "/api/apply-queue/pending")).json()
        return [ApplyRequest.model_validate(r) for r in rows]

    async def claim(self, queue_id: int) -> ApplyRequest:
        """Claim a request. Never retried: a lost response could mean we already own it."""
        response = await self._request("POST", f"/api/apply-queue/{queue_id}/claim", retry=False)
        return ApplyRequest.model_validate(response.json())

    async def download_document(self, queue_id: int, doc_type: str, fmt: str,
                                dest_dir: Path) -> Path:
        response = await self._request(
            "GET", f"/api/apply-queue/{queue_id}/documents/{doc_type}", params={"fmt": fmt})
        match = re.search(r'filename="?([^";]+)"?', response.headers.get("content-disposition", ""))
        filename = Path(match.group(1)).name if match else f"{doc_type}.{fmt}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / filename
        path.write_bytes(response.content)
        return path

    async def progress(self, queue_id: int, **fields: Any) -> None:
        body = {k: v for k, v in fields.items() if v is not None}
        await self._request("POST", f"/api/apply-queue/{queue_id}/progress", json=body)

    async def result(self, queue_id: int, status: str, notes: str | None = None) -> None:
        try:
            await self._request("POST", f"/api/apply-queue/{queue_id}/result",
                                json={"status": status, "notes": notes})
        except ConflictError:
            # A retry after a lost response lands here — the result was already recorded
            logger.info("Result for #%d was already recorded", queue_id)
