"""Orchestration — claim a request, open the application, fill it, wait for the human.

Per request:
  1. claim → 2. download resume + cover letter PDFs → 3. open the listing →
  4. click through to the application (never a submit control) →
  5. detect the ATS → 6-8. discover and fill fields → 9. overlay + highlights →
  10. wait for Done / Abandon (re-filling on each new step) → 11. report + clean up.

The human always clicks the ATS's own Next/Continue/Submit. Sign-in pages and
CAPTCHAs are handed off, never automated.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apply_client.ats.detector import detect, detect_url
from apply_client.ats.field_matcher import decide_action
from apply_client.ats.fillers import get_filler
from apply_client.browser import (
    attach_overlay, highlight_fields, launch_persistent_context, scroll_into_view, update_overlay,
)
from apply_client.config import ClientConfig
from apply_client.jetson_api import (
    AuthError, ConflictError, JetsonAPI, JetsonAPIError, NotConfiguredError,
)
from apply_client.models import ApplicantData, ApplyRequest, ATSType, FormField

logger = logging.getLogger(__name__)

# Listing-page "apply" buttons per source. Clicking these OPENS the application —
# they never submit anything (see _is_safe_apply_button).
SOURCE_APPLY_BUTTONS: dict[str, list[str]] = {
    "indeed": ["#indeedApplyButton", "button[id*='indeedApplyButton']",
               "[data-testid*='indeedApplyButton']", "a[href*='applystart']",
               "button:has-text('Apply now')", "a:has-text('Apply on company site')",
               "button:has-text('Apply on company site')"],
    "linkedin": ["button.jobs-apply-button", ".jobs-apply-button--top-card button",
                 "button:has-text('Easy Apply')", "a:has-text('Apply')"],
    "dice": ["apply-button-wc", "[data-cy='apply-button']", "button:has-text('Easy apply')",
             "button:has-text('Apply now')", "a:has-text('Apply now')"],
}
GENERIC_APPLY_BUTTONS = [
    "a:has-text('Apply for this job')", "button:has-text('Apply for this job')",
    "a:has-text('Apply now')", "button:has-text('Apply now')",
    "a:has-text('Apply')", "button:has-text('Apply')",
]
MODAL_SELECTORS = ".jobs-easy-apply-modal, [role='dialog']:visible, [class*='ia-Modal']"

USAJOBS_HANDOFF = ("USAJobs applications go through login.gov — the client never automates "
                   "that. Apply yourself, then click Done (or Abandon).")


class HandOff(Exception):
    """The human has to take over (couldn't find the apply button, login.gov, ...)."""


@dataclass
class Session:
    request: ApplyRequest
    applicant: ApplicantData
    documents: dict[str, Path]
    page: object = None
    ats: ATSType = ATSType.UNKNOWN
    filled: int = 0
    flagged: int = 0
    never: int = 0
    seen: set[str] = field(default_factory=set)

    @property
    def needs_you(self) -> int:
        return self.flagged + self.never

    def summary(self) -> str:
        url = getattr(self.page, "url", "") if self.page else ""
        return (f"{self.ats.value}: filled {self.filled}, needs you {self.flagged}, "
                f"sensitive skipped {self.never}" + (f" — {url}" if url else ""))


class ApplyRunner:
    def __init__(self, cfg: ClientConfig, api: JetsonAPI | None, context, console: Console) -> None:  # type: ignore[no-untyped-def]
        self.cfg = cfg
        self.api = api
        self.context = context
        self.console = console

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    async def run_watch(self) -> None:
        """Poll forever: claim the oldest pending request, process it, repeat."""
        assert self.api is not None
        self.console.print(f"[bold]Watching {self.cfg.jetson_url} every "
                           f"{self.cfg.poll_interval_seconds:.0f}s — Ctrl+C to stop[/bold]")
        while True:
            try:
                health = await self.api.health()
                if health.get("pending"):
                    pending = await self.api.get_pending()
                    if pending:
                        oldest = min(pending, key=lambda r: (r.queued_at or "", r.queue_id))
                        await self._claim_and_process(oldest.queue_id)
                        continue  # look again right away
            except (AuthError, NotConfiguredError) as e:
                self.console.print(f"[red]{e}[/red]\nCheck APPLY_CLIENT_TOKEN in apply_client/.env "
                                   "matches the Jetson's config/secrets.env.")
                raise SystemExit(1)
            except JetsonAPIError as e:
                self.console.print(f"[yellow]{e}[/yellow]")
            await asyncio.sleep(self.cfg.poll_interval_seconds)

    async def run_one(self, queue_id: int) -> None:
        assert self.api is not None
        await self._claim_and_process(queue_id)

    async def _claim_and_process(self, queue_id: int) -> None:
        assert self.api is not None
        try:
            request = await self.api.claim(queue_id)
        except ConflictError:
            self.console.print(f"[yellow]#{queue_id} was already claimed — skipping[/yellow]")
            return
        self.console.rule(f"[bold]#{request.queue_id} {request.job.title} @ {request.job.company}")
        await self.process(request)

    # ------------------------------------------------------------------
    # One request
    # ------------------------------------------------------------------

    async def process(self, request: ApplyRequest) -> None:
        """Handle a claimed request end to end. Always reports a result and cleans up."""
        assert self.api is not None
        qid = request.queue_id
        download_dir = self.cfg.download_dir / str(qid)
        opened: list = []

        def track_page(new_page) -> None:  # type: ignore[no-untyped-def]
            # A plain function: Playwright tags handlers, which builtins like list.append refuse
            opened.append(new_page)

        self.context.on("page", track_page)
        status, notes = "failed", ""
        session: Session | None = None
        try:
            if request.applicant is None:
                raise RuntimeError(f"Applicant data unavailable on the Jetson: {request.applicant_error}")

            documents: dict[str, Path] = {}
            for doc_type in ("resume", "cover_letter"):
                if doc_type in request.documents:
                    documents[doc_type] = await self.api.download_document(
                        qid, doc_type, "pdf", download_dir)
            self.console.print(f"  Downloaded: {', '.join(p.name for p in documents.values()) or 'nothing'}")

            session = Session(request=request, applicant=request.applicant, documents=documents)
            signals: asyncio.Queue[str] = asyncio.Queue()

            page = await self.context.new_page()
            await attach_overlay(page, signals.put_nowait)
            self.console.print(f"  Opening listing: {request.job.url}")
            await page.goto(request.job.url, wait_until="domcontentloaded", timeout=45000)
            session.page = page

            handoff: str | None = None
            source = (request.job.source or "").lower()
            if source == "usajobs" or "usajobs.gov" in page.url:
                handoff = USAJOBS_HANDOFF
            elif detect_url(page.url)[0] in (ATSType.UNKNOWN, ATSType.LINKEDIN_EASY):
                try:
                    session.page = await self._click_through(page, source, signals)
                except HandOff as e:
                    handoff = str(e)

            await self.api.progress(qid, status="in_progress", apply_url=session.page.url,
                                    notes=handoff)
            if handoff:
                self.console.print(f"  [yellow]Hand-off:[/yellow] {handoff}")
                await update_overlay(session.page, 0, 0, handoff)
            else:
                await self._fill_pass(session)

            status, notes = await self._wait_for_human(session, signals)
        except asyncio.CancelledError:
            status = "abandoned"
            notes = "Apply client stopped" + (f" — {session.summary()}" if session else "")
            raise
        except Exception as e:
            status = "failed"
            notes = f"{type(e).__name__}: {e}\n" + traceback.format_exc()[-800:]
            self.console.print(f"  [red]Failed:[/red] {e}")
        finally:
            self.context.remove_listener("page", track_page)
            try:
                await asyncio.shield(self.api.result(qid, status, notes[:2000]))
                self.console.print(f"  Reported [bold]{status}[/bold]")
            except Exception as e:
                self.console.print(f"  [red]Couldn't report result: {e}[/red]")
            for p in opened:
                try:
                    await p.close()
                except Exception:
                    pass
            shutil.rmtree(download_dir, ignore_errors=True)

    async def _click_through(self, page, source: str, signals: asyncio.Queue) -> object:  # type: ignore[no-untyped-def]
        """From the listing page to the application (modal, same tab, or new tab)."""
        selectors = SOURCE_APPLY_BUTTONS.get(source, []) + GENERIC_APPLY_BUTTONS
        button = None
        for _ in range(16):  # the listing may still be rendering
            for selector in selectors:
                candidate = page.locator(f"{selector} >> visible=true").first
                try:
                    if await candidate.count() and await _is_safe_apply_button(candidate):
                        button = candidate
                        break
                except Exception:
                    continue
            if button:
                break
            await asyncio.sleep(0.5)
        if button is None:
            raise HandOff("Couldn't find the apply button — click it yourself, then press Rescan")

        pages_before = set(self.context.pages)
        url_before = page.url
        self.console.print("  Clicking the listing's apply button")
        await button.click()

        for _ in range(40):  # up to 10s for a modal, navigation, or new tab
            await asyncio.sleep(0.25)
            new_pages = [p for p in self.context.pages if p not in pages_before]
            if new_pages:
                target = new_pages[-1]
                await target.wait_for_load_state("domcontentloaded", timeout=30000)
                await attach_overlay(target, signals.put_nowait)
                return target
            if page.url != url_before:
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                return page
            if await page.locator(MODAL_SELECTORS).count():
                return page
        raise HandOff("Clicked apply but nothing opened — continue yourself, then press Rescan")

    async def _fill_pass(self, session: Session) -> None:
        """Detect and fill the fields on the current step. Fields seen before are skipped."""
        assert self.api is not None
        page = session.page
        ats, confidence = await detect(page)
        session.ats = ats
        filler = get_filler(ats)

        blockers = await filler.detect_blockers(page)
        if blockers:
            message = "Needs you: " + "; ".join(blockers) + " — then press Rescan"
            self.console.print(f"  [yellow]{message}[/yellow]")
            await update_overlay(page, session.filled, session.needs_you, message)
            await self.api.progress(session.request.queue_id, status="in_progress",
                                    ats_detected=ats.value, apply_url=page.url, notes=message)
            return

        fields = [f for f in await filler.detect_fields(page) if f.key not in session.seen]
        session.seen.update(f.key for f in fields)
        if not fields:
            await update_overlay(page, session.filled, session.needs_you,
                                 "Nothing new to fill on this step — continue yourself")
            return

        result = await filler.fill(page, session.applicant, fields, session.documents)
        session.filled += len(result.filled)
        session.flagged += len(result.flagged)
        session.never += len(result.skipped_never)

        flagged_keys = {f.key for f in result.flagged}
        await highlight_fields([f for f in result.filled if f.key not in flagged_keys], "filled")
        await highlight_fields(result.flagged, "flagged")
        await highlight_fields(result.skipped_never, "never")
        first_attention = (result.flagged + result.skipped_never)[:1]
        if first_attention:
            await scroll_into_view(first_attention[0])

        message = ("Review the yellow/red fields, then continue or submit YOURSELF. "
                   "Click Done after you submit.")
        await update_overlay(page, session.filled, session.needs_you, message)
        await self.api.progress(session.request.queue_id, status="in_progress",
                                ats_detected=ats.value, apply_url=page.url,
                                fields_filled=session.filled, fields_flagged=session.needs_you)
        self._print_pass(ats, confidence, result.filled, result.flagged, result.skipped_never)

    async def _wait_for_human(self, session: Session, signals: asyncio.Queue) -> tuple[str, str]:
        """Until Done / Abandon / timeout. Re-fills on navigation, new tabs, and Rescan."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.human_timeout_minutes * 60

        def on_navigated(frame) -> None:  # type: ignore[no-untyped-def]
            if session.page is not None and frame == session.page.main_frame:
                signals.put_nowait("navigated")

        async def on_new_page(new_page) -> None:  # type: ignore[no-untyped-def]
            await new_page.wait_for_load_state("domcontentloaded")
            await attach_overlay(new_page, signals.put_nowait)
            new_page.on("framenavigated", on_navigated)
            session.page = new_page
            signals.put_nowait("navigated")

        session.page.on("framenavigated", on_navigated)
        self.context.on("page", on_new_page)
        self.console.print(f"  [bold]Your turn[/bold] — review, submit, then click Done in the "
                           f"overlay (timeout {self.cfg.human_timeout_minutes:.0f} min)")
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                kinds = [await asyncio.wait_for(signals.get(), timeout=remaining)]
                if kinds[0] == "navigated":
                    await asyncio.sleep(1.5)  # let the new step render
                while not signals.empty():  # collapse bursts; never lose a Done/Abandon
                    kinds.append(signals.get_nowait())
                if "done" in kinds:
                    return "completed", session.summary()
                if "abandon" in kinds:
                    return "abandoned", f"Abandoned by you — {session.summary()}"
                try:
                    await self._fill_pass(session)
                except Exception as e:  # a half-loaded step shouldn't kill the session
                    logger.debug("Fill pass failed", exc_info=True)
                    self.console.print(f"  [yellow]Fill pass skipped: {e}[/yellow]")
        except asyncio.TimeoutError:
            return "abandoned", (f"Timed out after {self.cfg.human_timeout_minutes:.0f} min "
                                 f"waiting for you — {session.summary()}")
        finally:
            self.context.remove_listener("page", on_new_page)

    def _print_pass(self, ats: ATSType, confidence, filled: list[FormField],  # type: ignore[no-untyped-def]
                    flagged: list[FormField], never: list[FormField]) -> None:
        self.console.print(f"  ATS: [bold]{ats.value}[/bold] ({confidence.name}) — "
                           f"filled {len(filled)}, needs you {len(flagged)}, "
                           f"sensitive {len(never)}")
        if flagged or never:
            table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
            table.add_column("Needs you", style="yellow")
            table.add_column("Why", style="dim")
            for f in flagged + never:
                table.add_row(f.label[:60] or f"({f.input_type})", f.note[:80])
            self.console.print(table)


async def _is_safe_apply_button(locator) -> bool:  # type: ignore[no-untyped-def]
    """A listing's Apply button — never a form submit control."""
    return await locator.evaluate(
        """(el) => {
            if ((el.getAttribute("type") || "").toLowerCase() === "submit") return false;
            const form = el.closest("form");
            if (form && form.querySelector(
                "input[type=text], input[type=email], input[type=tel], input[type=file], textarea"))
                return false;  // inside a filled-out form: could submit it
            return true;
        }"""
    )


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

async def dry_run(playwright, cfg: ClientConfig, url: str, console: Console) -> None:  # type: ignore[no-untyped-def]
    """Open a URL, detect ATS + fields, print what WOULD be filled. Fills nothing."""
    context = await launch_persistent_context(playwright, cfg)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
        ats, confidence = await detect(page)
        filler = get_filler(ats)
        console.print(f"ATS: [bold]{ats.value}[/bold] ({confidence.name}) — filler: "
                      f"{type(filler).__name__}")
        for reason in await filler.detect_blockers(page):
            console.print(f"[yellow]Blocker:[/yellow] {reason}")

        fields = await filler.detect_fields(page)
        table = Table(title=f"{len(fields)} fields on {page.url}")
        for col in ("Label", "Type", "Canonical", "Confidence", "Would"):
            table.add_column(col)
        colors = {"fill": "green", "fill+flag": "yellow", "upload": "green", "decline": "cyan",
                  "flag": "yellow", "never": "red"}
        by_action: dict[str, list[FormField]] = {}
        for f in fields:
            action = decide_action(f).value
            by_action.setdefault(action, []).append(f)
            table.add_row(f.label[:60] or f"[dim]{f.name or f.selector}[/dim]", f.input_type,
                          f.canonical or "—", f.confidence.name,
                          f"[{colors[action]}]{action}[/{colors[action]}]")
        console.print(table)

        await highlight_fields(by_action.get("fill", []) + by_action.get("upload", [])
                               + by_action.get("decline", []), "filled")
        await highlight_fields(by_action.get("flag", []) + by_action.get("fill+flag", []), "flagged")
        await highlight_fields(by_action.get("never", []), "never")
        console.print("Nothing was filled. Outlines in the browser show what would happen. "
                      "Press Enter to close.")
        await asyncio.get_running_loop().run_in_executor(None, input)
    finally:
        await context.close()
