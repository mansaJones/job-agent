"""Option matching — pure helpers plus native <select> and custom combobox (headless)."""

from __future__ import annotations

import pytest

from apply_client.ats.fillers.base import BaseFiller, choose_decline_option, choose_option
from apply_client.models import Confidence, FormField


@pytest.mark.parametrize("value, options, expected", [
    ("Yes", ["Select...", "Yes", "No"], 1),
    ("No", ["--", "Yes", "No"], 2),
    ("Yes, I am authorized to work in the United States", ["Yes", "No"], 0),
    ("No", ["No, I will not require sponsorship", "Yes, I will"], 0),
    ("No", ["Not applicable", "Yes"], None),         # "no" ≠ "not"
    ("Job board", ["LinkedIn", "Job Board (Indeed, Dice, etc.)", "Referral"], 1),
    ("Two weeks notice", ["Immediately", "1 month"], None),
    ("Yes", ["Please select", "Maybe"], None),        # never option[0]
    ("", ["Yes", "No"], None),
])
def test_choose_option(value: str, options: list[str], expected: int | None) -> None:
    assert choose_option(value, options) == expected


@pytest.mark.parametrize("options, expected", [
    (["Male", "Female", "Decline To Self Identify"], 2),
    (["Asian", "I don't wish to answer"], 1),
    (["Yes", "No", "Prefer not to say"], 2),
    (["I am a veteran", "I am not a veteran"], None),
])
def test_choose_decline_option(options: list[str], expected: int | None) -> None:
    assert choose_decline_option(options) == expected


def _field(selector: str, input_type: str, options: list[str] | None = None, frame=None):  # type: ignore[no-untyped-def]
    return FormField(selector=selector, label="q", input_type=input_type,
                     canonical="sponsorship_needed", confidence=Confidence.HIGH,
                     options=options or [], frame=frame)


async def test_native_select(page) -> None:  # type: ignore[no-untyped-def]
    await page.set_content("""<select id=s><option value="">Select...</option>
        <option value=y>Yes</option><option value=n>No</option></select>""")
    filler = BaseFiller()
    f = _field("#s", "select", ["Select...", "Yes", "No"], page.main_frame)
    assert await filler.select_option(page.main_frame, f, "No") is True
    assert await page.eval_on_selector("#s", "e => e.value") == "n"

    await page.eval_on_selector("#s", "e => e.value = ''")
    assert await filler.select_option(page.main_frame, f, "Maybe") is False
    assert await page.eval_on_selector("#s", "e => e.value") == ""  # untouched, not option[0]


CUSTOM_COMBOBOX = """
<div id=cb role=combobox tabindex=0 aria-expanded=false>Select one</div>
<ul id=lb role=listbox style="display:none">
  <li role=option>Yes</li><li role=option>No</li>
</ul>
<script>
  const cb = document.getElementById('cb'), lb = document.getElementById('lb');
  cb.addEventListener('click', () => { lb.style.display = 'block'; cb.setAttribute('aria-expanded', 'true'); });
  lb.addEventListener('click', e => { cb.textContent = e.target.textContent; lb.style.display = 'none'; });
  cb.addEventListener('keydown', e => { if (e.key === 'Escape') lb.style.display = 'none'; });
</script>"""


async def test_custom_combobox(page) -> None:  # type: ignore[no-untyped-def]
    await page.set_content(CUSTOM_COMBOBOX)
    filler = BaseFiller()
    f = _field("#cb", "combobox", frame=page.main_frame)
    assert await filler.select_option(page.main_frame, f, "Yes, I'm sure") is True
    assert await page.inner_text("#cb") == "Yes"

    await page.set_content(CUSTOM_COMBOBOX)
    assert await filler.select_option(page.main_frame, f, "Maybe") is False
    assert await page.inner_text("#cb") == "Select one"
    assert await page.is_hidden("#lb")  # closed again, nothing picked
