"""Base form filler — field discovery, matching, and safe filling.

SAFETY (enforced here for every ATS):
  - Never clicks Submit / Apply / Next / Continue / sign-in or any other
    final-step control. Every click goes through `_safe_click`, which refuses them.
  - Never touches sensitive fields (SSN, DOB, IDs, payment, signatures,
    consents, passwords) — they land in `FillResult.skipped_never`.
  - Never ticks checkboxes (usually agreements) and never guesses a dropdown
    option: no match → the field is flagged, never option[0].
  - Never overwrites a value that's already there (ATS prefill or the human).
  - EEO questions only ever get "decline to self-identify".
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from apply_client.ats.field_matcher import decide_action, match_field, normalize
from apply_client.models import (
    Action, ApplicantData, ATSType, Confidence, FillResult, FormField,
)

logger = logging.getLogger(__name__)

TEXT_TYPES = {"text", "email", "tel", "url", "number", "search", "textarea", "contenteditable"}
CHOICE_TYPES = {"select", "radio", "combobox"}

PLACEHOLDER_OPTIONS = {"", "select", "select one", "select an option", "please select",
                       "choose", "choose one", "choose an option", "none selected", "pick one"}
DECLINE_PHRASES = ["decline", "prefer not", "dont wish", "do not wish", "choose not",
                   "not to answer", "not to disclose", "not to say", "rather not"]

# Controls the client must never click, whatever the ATS
DANGEROUS_CONTROL = re.compile(
    r"\b(submit|apply|send|finish|finalize|confirm|review|next|continue|proceed|save|"
    r"sign in|signin|log in|login|sign up|register|create account|pay|purchase|agree)\b",
    re.IGNORECASE,
)

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming",
}


class UnsafeClickError(RuntimeError):
    """Refused to click a control that could submit or advance the application."""


# ---------------------------------------------------------------------------
# In-page field discovery
# ---------------------------------------------------------------------------

DISCOVER_JS = r"""
() => {
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim().slice(0, 300);
  const visible = (el) => {
    if (el.disabled) return false;
    const s = getComputedStyle(el);
    if (s.visibility === "hidden" || s.display === "none") return false;
    return el.getClientRects().length > 0;
  };
  const textOf = (el) => {
    if (!el) return "";
    const clone = el.cloneNode(true);
    clone.querySelectorAll("input, select, textarea, option, script, style").forEach(n => n.remove());
    return clean(clone.textContent);
  };
  const byIds = (ids) => clean((ids || "").split(/\s+/).map(id => textOf(document.getElementById(id))).join(" "));
  const LABEL_SEL = "label, legend, .label, [class*='label'], [class*='Label'], h3, h4";
  // Walk up from el looking for a label-ish element, but stop before a container
  // that also holds other fields (its label would belong to someone else).
  const nearbyLabel = (el, groupName) => {
    for (let a = el.parentElement, depth = 0; a && depth < 6; a = a.parentElement, depth++) {
      const controls = Array.from(a.querySelectorAll("input:not([type=hidden]), select, textarea"));
      const foreign = controls.filter(c => c !== el && !(groupName && c.name === groupName));
      if (foreign.length) break;
      for (const lab of a.querySelectorAll(LABEL_SEL)) {
        if (lab.contains(el) && lab.tagName !== "LABEL") continue;
        if (groupName && lab.querySelector("input")) continue;  // an option's own label
        const t = textOf(lab);
        if (t) return t;
      }
    }
    return "";
  };
  const labelFor = (el) => {
    if (el.labels && el.labels.length) {
      const t = clean(Array.from(el.labels).map(textOf).join(" "));
      if (t) return t;
    }
    const lb = byIds(el.getAttribute("aria-labelledby"));
    if (lb) return lb;
    const aria = el.getAttribute("aria-label");
    if (aria) return clean(aria);
    const near = nearbyLabel(el, null);
    if (near) return near;
    let prev = el.previousElementSibling;
    for (let i = 0; prev && i < 3; i++, prev = prev.previousElementSibling) {
      const t = textOf(prev);
      if (t) return t;
    }
    return "";
  };
  const groupLabel = (radio) => {
    const fs = radio.closest("fieldset");
    if (fs) {
      const lg = fs.querySelector("legend");
      if (lg) return textOf(lg);
    }
    const rg = radio.closest("[role='radiogroup']");
    if (rg) {
      const t = byIds(rg.getAttribute("aria-labelledby")) || clean(rg.getAttribute("aria-label"));
      if (t) return t;
    }
    return nearbyLabel(radio, radio.name || "__none__");
  };
  const ancestorInfo = (el) => {
    const ids = [], autos = [];
    for (let a = el.parentElement; a && ids.length + autos.length < 12; a = a.parentElement) {
      if (a.id) ids.push(a.id);
      const au = a.getAttribute && a.getAttribute("data-automation-id");
      if (au) autos.push(au);
    }
    return { ids, autos };
  };
  window.__jobAgentNextId = window.__jobAgentNextId || 1;
  window.__jobAgentDoc = window.__jobAgentDoc || Math.random().toString(36).slice(2, 8);
  const tag = (el) => {
    if (!el.dataset.jobagentId) el.dataset.jobagentId = window.__jobAgentDoc + '-' + (window.__jobAgentNextId++);
    return `[data-jobagent-id="${el.dataset.jobagentId}"]`;
  };

  const out = [];
  const radioGroups = new Map();
  const nodes = document.querySelectorAll(
    "input:not([type=hidden]), select, textarea, [role=combobox], [contenteditable=true]"
  );
  for (const el of nodes) {
    const tagName = el.tagName.toLowerCase();
    let type;
    if (tagName === "select") type = "select";
    else if (tagName === "textarea") type = "textarea";
    else if (el.getAttribute("role") === "combobox") type = "combobox";
    else if (el.isContentEditable && tagName !== "input") type = "contenteditable";
    else type = (el.getAttribute("type") || "text").toLowerCase();
    if (["submit", "button", "reset", "image"].includes(type)) continue;
    if (type !== "file" && !visible(el)) continue;
    if (el.closest("#job-agent-overlay")) continue;

    const anc = ancestorInfo(el);
    const base = {
      name: el.getAttribute("name") || "",
      id: el.id || "",
      aria: el.getAttribute("aria-label") || "",
      placeholder: el.getAttribute("placeholder") || "",
      required: !!(el.required || el.getAttribute("aria-required") === "true"),
      automationId: el.getAttribute("data-automation-id") || "",
      ancestorIds: anc.ids, ancestorAutomationIds: anc.autos,
    };

    if (type === "radio") {
      const key = el.name || tag(el);
      let g = radioGroups.get(key);
      if (!g) {
        g = { ...base, type: "radio", selector: tag(el), label: groupLabel(el),
              options: [], optionSelectors: [], value: "" };
        radioGroups.set(key, g);
        out.push(g);
      }
      const optLabel = labelFor(el) || clean(el.value);
      g.options.push(optLabel);
      g.optionSelectors.push(tag(el));
      if (el.checked) g.value = optLabel;
      g.required = g.required || base.required;
      continue;
    }

    let value = "", options = [];
    if (type === "select") {
      options = Array.from(el.options).map(o => clean(o.textContent));
      const sel = el.options[el.selectedIndex];
      value = sel && sel.value ? clean(sel.textContent) : "";
    } else if (type === "checkbox") {
      value = el.checked ? "checked" : "";
    } else if (type === "file") {
      value = el.files && el.files.length ? el.files[0].name : "";
    } else if (type === "contenteditable") {
      value = clean(el.innerText);
    } else if (type === "combobox" && tagName !== "input") {
      value = "";  // custom dropdown button; its text is usually a placeholder
    } else {
      value = el.value || "";
    }
    out.push({ ...base, type, selector: tag(el), label: labelFor(el), options, value });
  }
  return out;
}
"""

BLOCKERS_JS = r"""
() => {
  const reasons = [];
  const vis = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  if (Array.from(document.querySelectorAll("input[type=password]")).some(vis))
    reasons.push("Sign-in required — log in yourself (the client never types credentials)");
  const captcha = document.querySelector(
    "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], " +
    "iframe[title*='captcha' i], .g-recaptcha, .h-captcha, .cf-turnstile, [class*='captcha' i]"
  );
  if (captcha && vis(captcha)) reasons.push("CAPTCHA on the page — solve it yourself");
  return reasons;
}
"""


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    return normalize(text).split()


def is_placeholder_option(text: str) -> bool:
    n = normalize(text)
    return n in PLACEHOLDER_OPTIONS or n.startswith("select ") or n.startswith("please ") \
        or set(n) <= {"-", " "}


def choose_option(value: str, options: list[str]) -> int | None:
    """Index of the option matching value, or None. Never falls back to option 0.

    Tiers: exact → word-prefix either way ("Yes, I am authorized…" ↔ "Yes") →
    word containment (only for values/options long enough to be unambiguous).
    """
    val = _tokens(value)
    if not val:
        return None
    candidates = [(i, _tokens(o)) for i, o in enumerate(options) if not is_placeholder_option(o)]

    for i, opt in candidates:
        if opt == val:
            return i
    for i, opt in candidates:
        if opt and (opt[:len(val)] == val or val[:len(opt)] == opt):
            return i

    def contains(outer: list[str], inner: list[str]) -> bool:
        n = len(inner)
        return any(outer[k:k + n] == inner for k in range(len(outer) - n + 1))

    for i, opt in candidates:
        shorter = opt if len(opt) <= len(val) else val
        if len(shorter) >= 2 or len(" ".join(shorter)) >= 5:
            if contains(opt, val) or contains(val, opt):
                return i
    return None


def choose_decline_option(options: list[str]) -> int | None:
    """Index of a "decline to self-identify"-style option, or None."""
    for i, option in enumerate(options):
        n = normalize(option)
        if any(phrase in n for phrase in DECLINE_PHRASES):
            return i
    return None


def value_for(canonical: str, applicant: ApplicantData) -> str | None:
    """The applicant's value for a canonical field. None/"" means nothing configured."""
    answers = applicant.answers.model_dump()
    if canonical in answers:
        return answers[canonical] or None
    if canonical == "location":
        loc = applicant.location or ", ".join(p for p in (applicant.city, applicant.state) if p)
        return loc or None
    if canonical == "years_experience":
        return str(applicant.years_experience) if applicant.years_experience else None
    value = getattr(applicant, canonical, None)
    return str(value) if value else None


def _same_value(a: str, b: str) -> bool:
    return normalize(a) == normalize(b) or re.sub(r"\D", "", a) == re.sub(r"\D", "", b) != ""


# ---------------------------------------------------------------------------
# Filler
# ---------------------------------------------------------------------------

class BaseFiller:
    ats: ATSType = ATSType.UNKNOWN

    # ---- discovery ---------------------------------------------------------

    async def detect_fields(self, page) -> list[FormField]:  # type: ignore[no-untyped-def]
        """Every visible fillable field in every frame, matched to a canonical."""
        fields: list[FormField] = []
        for frame in page.frames:
            try:
                raw_fields = await frame.evaluate(DISCOVER_JS)
            except Exception as e:  # detached or navigating frame
                logger.debug("Field discovery skipped a frame (%s): %s", frame.url, e)
                continue
            for raw in raw_fields:
                f = self._to_field(raw, frame)
                self.classify(f, raw)
                fields.append(f)
        return fields

    def _to_field(self, raw: dict[str, Any], frame: Any) -> FormField:
        canonical, confidence = match_field(
            raw.get("label"), raw.get("name"), raw.get("id"), raw.get("aria"),
            raw.get("placeholder"),
        )
        if raw["type"] == "password":
            canonical, confidence = "_never_credentials", Confidence.HIGH
        if raw["type"] in ("textarea", "contenteditable") and confidence < Confidence.HIGH \
                and canonical and not canonical.startswith("_"):
            confidence = Confidence.LOW  # free-text answers are always the human's
        return FormField(
            selector=raw["selector"], label=raw.get("label") or "", input_type=raw["type"],
            canonical=canonical, confidence=confidence, name=raw.get("name") or "",
            element_id=raw.get("id") or "", required=bool(raw.get("required")),
            options=raw.get("options") or [], option_selectors=raw.get("optionSelectors") or [],
            current_value=raw.get("value") or "", frame=frame,
            extra={"automation_id": raw.get("automationId") or "",
                   "ancestor_ids": raw.get("ancestorIds") or [],
                   "ancestor_automation_ids": raw.get("ancestorAutomationIds") or []},
        )

    def classify(self, f: FormField, raw: dict[str, Any]) -> None:
        """ATS-specific overrides of canonical/confidence. Base: no changes."""

    async def detect_blockers(self, page) -> list[str]:  # type: ignore[no-untyped-def]
        """Reasons the human must take over this page (sign-in, CAPTCHA)."""
        reasons: list[str] = []
        for frame in page.frames:
            try:
                reasons += await frame.evaluate(BLOCKERS_JS)
            except Exception:
                continue
        return list(dict.fromkeys(reasons))

    # ---- filling -----------------------------------------------------------

    async def fill(
        self,
        page,  # type: ignore[no-untyped-def]
        applicant: ApplicantData,
        fields: list[FormField],
        documents: dict[str, Path] | None = None,
    ) -> FillResult:
        """Fill fields per the confidence policy. Text first, then choices, files last."""
        documents = documents or {}
        result = FillResult()

        def order(f: FormField) -> int:
            if f.input_type == "file":
                return 2  # some ATSs re-render the form after an upload
            return 1 if f.input_type in CHOICE_TYPES else 0

        for f in sorted(fields, key=order):
            action = decide_action(f)
            try:
                await self._apply(f, action, applicant, documents, result)
            except UnsafeClickError as e:
                f.note = str(e)
                result.flagged.append(f)
                logger.warning("Refused unsafe click on %r: %s", f.label, e)
            except Exception as e:
                f.note = f"Couldn't fill: {e}".split("\n")[0][:200]
                result.flagged.append(f)
                logger.debug("Fill failed for %r", f.label, exc_info=True)
        return result

    async def _apply(self, f: FormField, action: Action, applicant: ApplicantData,
                     documents: dict[str, Path], result: FillResult) -> None:
        if action == Action.NEVER:
            f.note = "Sensitive field — never filled by the client"
            result.skipped_never.append(f)
            return

        if action == Action.FLAG:
            if not f.note:
                f.note = ("Checkbox — tick it yourself if you agree" if f.input_type == "checkbox"
                          else "EEO question — no decline option, answer yourself"
                          if f.canonical == "_eeo"
                          else "Unrecognized question" if not f.canonical
                          else "Low confidence — check this one")
            result.flagged.append(f)
            return

        if action == Action.DECLINE:
            if await self.select_decline_option(f.frame, f):
                f.note = "Declined to self-identify"
                result.filled.append(f)
            else:
                f.note = "EEO question with no decline option — answer or skip yourself"
                result.flagged.append(f)
            return

        if action == Action.UPLOAD:
            path = documents.get(f.canonical or "")
            if path is None:
                f.note = f"No {f.canonical} document available"
                result.flagged.append(f)
            elif f.current_value:
                f.note = f"Already has a file ({f.current_value}) — left as is"
                result.flagged.append(f)
            elif await self.upload_file(f.frame, f, path):
                f.note = f"Uploaded {path.name}"
                result.filled.append(f)
            else:
                f.note = "Upload didn't stick — attach it yourself"
                result.flagged.append(f)
            return

        # FILL / FILL_AND_FLAG
        value = value_for(f.canonical or "", applicant)
        if not value:
            f.note = f"No value configured for {f.canonical}"
            result.flagged.append(f)
            return

        if f.current_value:
            if _same_value(f.current_value, value) or (
                    f.input_type in CHOICE_TYPES and choose_option(value, [f.current_value]) is not None):
                f.note = "Already filled"
                result.filled.append(f)
            else:
                f.note = f"Prefilled with {f.current_value!r} — left as is, check it"
                result.flagged.append(f)
            return

        if f.input_type in CHOICE_TYPES:
            ok = await self.select_option(f.frame, f, value)
        elif f.input_type in TEXT_TYPES:
            await f.frame.locator(f.selector).fill(value)
            ok = True
        else:
            f.note = f"Unsupported field type {f.input_type}"
            ok = False

        if not ok:
            if not f.note:
                f.note = f"No option matching {value!r}"
            result.flagged.append(f)
            return
        result.filled.append(f)
        if action == Action.FILL_AND_FLAG:
            f.note = f"Filled {value!r} (medium confidence) — check it"
            result.flagged.append(f)

    # ---- primitives --------------------------------------------------------

    async def _safe_click(self, locator) -> None:  # type: ignore[no-untyped-def]
        """Click unless the element looks like a submit / advance / sign-in control."""
        info = await locator.evaluate(
            "(el) => ({tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),"
            " role: el.getAttribute('role') || '',"
            " text: [el.innerText, el.value, el.getAttribute('aria-label')].filter(Boolean).join(' ').slice(0, 120)})"
        )
        is_button_like = info["tag"] in ("button", "a") or info["type"] in ("submit", "button") \
            or info["role"] in ("button", "link")
        if info["type"] == "submit" or (is_button_like and info["role"] != "combobox"
                                        and DANGEROUS_CONTROL.search(info["text"] or "")):
            raise UnsafeClickError(f"Refused to click {info['text']!r} — that's the human's job")
        await locator.click()

    async def select_option(self, frame, f: FormField, value: str) -> bool:  # type: ignore[no-untyped-def]
        """Choose the option matching value. Returns False (never guesses) if none match."""
        candidates = [value]
        if f.canonical == "state" and value.upper() in US_STATES:
            candidates.append(US_STATES[value.upper()])
        if f.canonical in ("location", "city"):
            # "Homewood, IL" should match "Homewood, Illinois, United States"
            parts = [p.strip() for p in value.split(",")]
            if len(parts) >= 2 and parts[1].upper() in US_STATES:
                candidates.append(f"{parts[0]}, {US_STATES[parts[1].upper()]}")

        if f.input_type == "select":
            for candidate in candidates:
                idx = choose_option(candidate, f.options)
                if idx is not None:
                    await frame.locator(f.selector).select_option(index=idx)
                    return True
            return False

        if f.input_type == "radio":
            for candidate in candidates:
                idx = choose_option(candidate, f.options)
                if idx is not None and idx < len(f.option_selectors):
                    await frame.locator(f.option_selectors[idx]).check(force=True)
                    return True
            return False

        # Autocomplete inputs: type just the first part ("Homewood") so the list populates
        type_text = value.split(",")[0].strip() if await self._is_text_combobox(frame, f) else None
        return await self._choose_from_listbox(frame, f, lambda opts: next(
            (i for c in candidates if (i := choose_option(c, opts)) is not None), None),
            type_text=type_text)

    async def select_decline_option(self, frame, f: FormField) -> bool:  # type: ignore[no-untyped-def]
        """Pick "decline to self-identify" (or similar). False if no such option exists."""
        if f.input_type == "select":
            idx = choose_decline_option(f.options)
            if idx is None:
                return False
            await frame.locator(f.selector).select_option(index=idx)
            return True
        if f.input_type == "radio":
            idx = choose_decline_option(f.options)
            if idx is None or idx >= len(f.option_selectors):
                return False
            await frame.locator(f.option_selectors[idx]).check(force=True)
            return True
        if f.input_type == "combobox":
            return await self._choose_from_listbox(frame, f, choose_decline_option)
        return False

    async def _is_text_combobox(self, frame, f: FormField) -> bool:  # type: ignore[no-untyped-def]
        return await frame.locator(f.selector).evaluate(
            "(el) => el.tagName === 'INPUT' && (el.type || 'text') === 'text'")

    async def _choose_from_listbox(self, frame, f: FormField, chooser,  # type: ignore[no-untyped-def]
                                   type_text: str | None = None) -> bool:
        """Open a custom dropdown ([role=combobox] / Workday button) and click the chosen option."""
        box = frame.locator(f.selector)
        if type_text is not None:
            await box.fill(type_text)  # autocomplete input: typing opens the list
        else:
            await self._safe_click(box)
        options = frame.locator("[role=option]:visible")
        try:
            await options.first.wait_for(state="visible", timeout=2500)
        except Exception:
            await box.press("Escape")
            return False
        texts = [t.strip() for t in await options.all_inner_texts()]
        idx = chooser(texts)
        if idx is None:
            await box.press("Escape")
            return False
        await self._safe_click(options.nth(idx))
        return True

    async def upload_file(self, frame, f: FormField, path: Path) -> bool:  # type: ignore[no-untyped-def]
        """Set the real <input type=file> (even if hidden), then verify it stuck.

        Some ATSs (Workday, Indeed) replace the input after upload, so re-query
        before declaring success.
        """
        await frame.locator(f.selector).set_input_files(str(path))
        await frame.wait_for_timeout(500)
        return await frame.evaluate(
            """([sel, name]) => {
                const el = document.querySelector(sel);
                if (el && el.files && el.files.length) return true;
                const anyFile = Array.from(document.querySelectorAll("input[type=file]"))
                    .some(i => i.files && Array.from(i.files).some(x => x.name === name));
                return anyFile || document.body.innerText.includes(name);
            }""",
            [f.selector, path.name],
        )
