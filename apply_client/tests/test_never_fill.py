"""Sensitive fields are never filled and never-click controls are never clicked (headless)."""

from __future__ import annotations

import pytest

from apply_client.ats.fillers import get_filler
from apply_client.ats.fillers.base import UnsafeClickError
from apply_client.models import ATSType
from apply_client.runner import _is_safe_apply_button

SENSITIVE_FORM = """
<form onsubmit="window.__submitted = true; return false;">
  <label for=fn>First name</label><input id=fn name=first_name>
  <label for=ssn>Social Security Number</label><input id=ssn name=ssn>
  <label for=dob>Date of birth</label><input id=dob name=dob type=date>
  <label for=sig>Type your full name as your electronic signature</label><input id=sig name=sig>
  <label for=pw>Password</label><input id=pw name=pw type=password>
  <label><input type=checkbox id=agree> I agree to the terms</label>
  <button type=submit id=go>Submit application</button>
</form>"""


async def test_sensitive_fields_skipped(page, applicant) -> None:  # type: ignore[no-untyped-def]
    await page.set_content(SENSITIVE_FORM)
    filler = get_filler(ATSType.UNKNOWN)
    result = await filler.fill(page, applicant, await filler.detect_fields(page))

    skipped = {f.canonical for f in result.skipped_never}
    assert {"_never_ssn", "_never_dob", "_never_signature", "_never_credentials",
            "_never_consent"} <= skipped
    assert [f.label for f in result.filled] == ["First name"]
    for field_id in ("ssn", "dob", "sig", "pw"):
        assert await page.evaluate(f"document.getElementById('{field_id}').value") == ""
    assert await page.evaluate("document.getElementById('agree').checked") is False
    assert await page.evaluate("!!window.__submitted") is False


@pytest.mark.parametrize("html", [
    "<button id=b type=submit>Go</button>",
    "<button id=b type=button>Submit application</button>",
    "<a id=b href='#'>Continue</a>",
    "<button id=b>Next</button>",
    "<div id=b role=button>Sign in</div>",
])
async def test_safe_click_refuses_advance_controls(page, html: str) -> None:  # type: ignore[no-untyped-def]
    await page.set_content(f"<form onsubmit='window.__submitted=true;return false'>{html}</form>"
                           "<script>document.getElementById('b').onclick=()=>window.__clicked=true</script>")
    with pytest.raises(UnsafeClickError):
        await get_filler(ATSType.UNKNOWN)._safe_click(page.locator("#b"))
    assert await page.evaluate("!!window.__clicked || !!window.__submitted") is False


async def test_listing_apply_button_inside_form_refused(page) -> None:  # type: ignore[no-untyped-def]
    await page.set_content("""
        <a id=listing href="#">Apply now</a>
        <form><input type=text name=q><button id=inform type=button>Apply</button></form>""")
    assert await _is_safe_apply_button(page.locator("#listing")) is True
    assert await _is_safe_apply_button(page.locator("#inform")) is False
