"""Browser helpers — persistent Chrome profile, status overlay, field highlighting.

The persistent profile is the point: log in to Workday / Greenhouse / Indeed /
LinkedIn once by hand in this profile and the sessions stick. The client never
types credentials.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from apply_client.config import ClientConfig
from apply_client.models import FormField

logger = logging.getLogger(__name__)

OVERLAY_JS = (Path(__file__).parent / "overlay.js").read_text(encoding="utf-8")

HIGHLIGHT_STYLES = {
    "filled": "2px solid rgba(34, 197, 94, 0.55)",
    "flagged": "3px solid #eab308",
    "never": "3px solid #dc2626",
}

SignalHandler = Callable[[str], Awaitable[None] | None]


async def launch_persistent_context(playwright, cfg: ClientConfig):  # type: ignore[no-untyped-def]
    """Real Chrome with the persistent profile; bundled Chromium (with a warning) as fallback."""
    cfg.chrome_profile_dir.mkdir(parents=True, exist_ok=True)
    common = dict(user_data_dir=str(cfg.chrome_profile_dir), headless=False, viewport=None,
                  args=["--start-maximized"], accept_downloads=False)
    try:
        return await playwright.chromium.launch_persistent_context(channel="chrome", **common)
    except Exception as e:
        logger.warning("Chrome channel unavailable (%s) — falling back to bundled Chromium. "
                       "Run `playwright install chrome` for your real Chrome.", str(e).split("\n")[0])
        return await playwright.chromium.launch_persistent_context(**common)


async def attach_overlay(page, handler: SignalHandler) -> None:  # type: ignore[no-untyped-def]
    """Expose the signal bridge and keep the overlay injected across navigations."""

    async def bridge(kind: str) -> None:
        # A plain function — Playwright can't wrap bound methods like Queue.put_nowait
        outcome = handler(str(kind))
        if outcome is not None and hasattr(outcome, "__await__"):
            await outcome

    await page.expose_function("__jobAgentSignal", bridge)

    async def reinject(frame) -> None:  # type: ignore[no-untyped-def]
        if frame == page.main_frame:
            await inject_overlay(page)

    page.on("framenavigated", reinject)  # Playwright schedules async handlers itself
    await inject_overlay(page)


async def inject_overlay(page) -> None:  # type: ignore[no-untyped-def]
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await page.add_script_tag(content=OVERLAY_JS)
    except Exception as e:  # navigating again, or a page that blocks inline scripts
        logger.debug("Overlay injection skipped: %s", e)


async def update_overlay(page, filled: int, flagged: int, message: str) -> None:  # type: ignore[no-untyped-def]
    try:
        if not await page.evaluate("typeof window.__jobAgentUpdate === 'function'"):
            await inject_overlay(page)
        await page.evaluate("([f, g, m]) => window.__jobAgentUpdate && window.__jobAgentUpdate(f, g, m)",
                            [filled, flagged, message])
    except Exception as e:
        logger.debug("Overlay update skipped: %s", e)


async def highlight_fields(fields: list[FormField], kind: str) -> None:
    """Outline fields: faint green = filled, yellow = needs you, red = sensitive (never filled)."""
    style = HIGHLIGHT_STYLES[kind]
    for f in fields:
        selectors = f.option_selectors or [f.selector]
        for selector in selectors:
            try:
                await f.frame.evaluate(
                    """([sel, style, note]) => {
                        const el = document.querySelector(sel);
                        if (!el) return;
                        const target = el.type === "file" || el.type === "radio"
                            ? (el.closest("label, .field, li, div") || el) : el;
                        target.style.outline = style;
                        target.style.outlineOffset = "2px";
                        if (note) target.title = "Job Agent: " + note;
                    }""",
                    [selector, style, f.note],
                )
            except Exception:
                continue


async def scroll_into_view(f: FormField) -> None:
    try:
        await f.frame.locator((f.option_selectors or [f.selector])[0]).scroll_into_view_if_needed(
            timeout=3000)
    except Exception:
        pass
