"""Telegram bot — sends job digest notifications and alerts.

Uses the Telegram Bot API directly via httpx (no extra dependencies).
"""

from __future__ import annotations

import html
import logging
from typing import Any

import httpx

from app.config import SecretsConfig
from app.database import Database

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    """Sends messages to a Telegram chat via the Bot API."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"{TELEGRAM_API}/bot{bot_token}"
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def from_secrets(cls, secrets: SecretsConfig) -> TelegramNotifier | None:
        """Create from secrets config. Returns None if not configured."""
        if not secrets.telegram_bot_token or not secrets.telegram_chat_id:
            logger.warning("Telegram not configured — missing bot token or chat ID")
            return None
        return cls(secrets.telegram_bot_token, secrets.telegram_chat_id)

    async def __aenter__(self) -> TelegramNotifier:
        self._client = httpx.AsyncClient(timeout=30)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("TelegramNotifier not initialized — use async with")
        return self._client

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message to the configured chat. Returns True on success."""
        try:
            resp = await self.client.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error("Telegram API error: %s", data.get("description", "unknown"))
                return False
            logger.debug("Telegram message sent to chat %s", self.chat_id)
            return True
        except httpx.HTTPError as e:
            logger.error("Telegram send failed: %s", e)
            return False

    async def send_daily_digest(self, db: Database, dashboard_url: str = "") -> bool:
        """Build and send a daily digest of job search activity."""
        stats = await db.get_stats()

        total = stats.get("total", 0)
        reviewed = stats.get("evaluated", 0)
        maybe = stats.get("maybe", 0)
        rejected = stats.get("rejected", 0)
        approved = stats.get("approved", 0)
        applied = stats.get("applied", 0)
        new = stats.get("new", 0)

        # Get top matches for the digest
        top_jobs, _ = await db.get_jobs_paginated(
            status="evaluated", limit=5, sort_by="eval_score", sort_dir="DESC"
        )

        # Build message
        lines = [
            "<b>📊 Job Agent — Daily Digest</b>",
            "",
            f"<b>Total jobs:</b> {total}",
            f"🟢 Ready for review: <b>{reviewed}</b>",
            f"🟡 Maybe: <b>{maybe}</b>",
            f"🔴 Rejected: <b>{rejected}</b>",
            f"✅ Approved: <b>{approved}</b>",
            f"📨 Applied: <b>{applied}</b>",
            f"🆕 Unevaluated: <b>{new}</b>",
        ]

        if top_jobs:
            lines.append("")
            lines.append("<b>🏆 Top Matches:</b>")
            for job in top_jobs:
                score = job.get("eval_score")
                score_str = f"{score:.2f}" if score is not None else "—"
                company = job.get("company") or "Unknown"
                title = job.get("title", "Untitled")
                lines.append(f"  • <b>{score_str}</b> — {title} @ {company}")

        if dashboard_url:
            lines.append("")
            lines.append(f'<a href="{dashboard_url}">Open Dashboard</a>')

        message = "\n".join(lines)
        return await self.send_message(message)

    async def send_scrape_summary(
        self, board_name: str, inserted: int, total_found: int
    ) -> bool:
        """Notify after a scrape completes."""
        message = (
            f"<b>🔍 Scrape Complete — {board_name}</b>\n"
            f"Found {total_found} jobs, {inserted} new listings saved."
        )
        return await self.send_message(message)

    async def send_eval_summary(
        self, evaluated: int, review: int, maybe: int, rejected: int
    ) -> bool:
        """Notify after an evaluation batch completes."""
        message = (
            f"<b>🤖 Evaluation Complete</b>\n"
            f"Evaluated {evaluated} jobs:\n"
            f"  🟢 Review: {review}\n"
            f"  🟡 Maybe: {maybe}\n"
            f"  🔴 Rejected: {rejected}"
        )
        return await self.send_message(message)

    async def send_apply_result(
        self,
        job_title: str,
        company: str,
        status: str,
        fields_filled: int | None,
        fields_flagged: int | None,
        notes: str | None,
    ) -> bool:
        """Notify when the apply client finishes a request."""
        title, company_s = html.escape(job_title or "?"), html.escape(company or "?")
        if status == "completed":
            message = (
                f"✅ <b>Applied</b> — {title} @ {company_s} "
                f"({fields_filled or 0} auto-filled, {fields_flagged or 0} manual)"
            )
        else:
            message = (
                f"⚠️ <b>Apply {html.escape(status)}</b> — {title} @ {company_s}: "
                f"{html.escape(notes or 'no notes')}"
            )
        return await self.send_message(message)

    async def send_alert(self, title: str, body: str) -> bool:
        """Send a generic alert (errors, warnings, etc)."""
        message = f"<b>⚠️ {title}</b>\n{body}"
        return await self.send_message(message)

    async def test_connection(self) -> bool:
        """Send a test message to verify the bot is working."""
        return await self.send_message(
            "✅ <b>Job Agent connected!</b>\n"
            "You'll receive daily digests and scrape/eval summaries here."
        )
