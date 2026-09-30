"""Greenhouse and Lever fixture forms — discovery, fill results, and no submit (headless)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apply_client.ats.fillers import get_filler
from apply_client.models import ATSType
from conftest import load_fixture

FIELD_VALUES_JS = """() => Object.fromEntries(Array.from(
    document.querySelectorAll('input:not([type=file]):not([type=radio]):not([type=checkbox]):not([type=submit]), select, textarea'))
    .map(e => [e.name || e.id, e.value]))"""


@pytest.fixture
def documents(tmp_path: Path) -> dict[str, Path]:
    resume, letter = tmp_path / "Lovelace_resume.pdf", tmp_path / "Lovelace_cover_letter.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    letter.write_bytes(b"%PDF-1.4 letter")
    return {"resume": resume, "cover_letter": letter}


def _by_label(fields):  # type: ignore[no-untyped-def]
    return {f.label.split("*")[0].strip(): f for f in fields}


async def test_greenhouse(page, applicant, documents) -> None:  # type: ignore[no-untyped-def]
    await load_fixture(page, "greenhouse_form.html")
    filler = get_filler(ATSType.GREENHOUSE)
    fields = await filler.detect_fields(page)

    canon = {label: f.canonical for label, f in _by_label(fields).items()}
    assert canon["First Name"] == "first_name"
    assert canon["Email"] == "email"
    assert canon["Resume/CV"] == "resume"
    assert canon["Cover Letter"] == "cover_letter"
    assert canon["LinkedIn Profile"] == "linkedin_url"
    assert canon["Are you legally authorized to work in the United States?"] == "work_authorization"
    assert canon["I certify that the information above is accurate"] == "_never_consent"
    assert canon["Gender"] == canon["Race"] == canon["Veteran Status"] == "_eeo"
    assert canon["Why do you want to work at Acme?"] is None

    result = await filler.fill(page, applicant, fields, documents)
    # 12 filled: 5 contact fields incl. location, LinkedIn, 2 yes/no, 2 EEO declines, 2 uploads
    assert len(result.filled) == 12
    # 6 need a look: location + 2 yes/no (medium), free text, salary (no value), veteran (no decline)
    assert len(result.flagged) == 6
    assert [f.label for f in result.skipped_never] == ["I certify that the information above is accurate"]

    values = await page.evaluate(FIELD_VALUES_JS)
    assert values["job_application[first_name]"] == "Ada"
    assert values["job_application[location]"] == "Homewood, Illinois, United States"
    assert values["job_application[answers_attributes][1][boolean_value]"] == "1"   # Yes
    assert values["job_application[answers_attributes][2][boolean_value]"] == "0"   # No sponsorship
    assert values["job_application[answers_attributes][3][text_value]"] == ""      # free text untouched
    assert values["job_application[gender]"] == "3"          # Decline To Self Identify
    assert values["job_application[veteran_status]"] == ""   # no decline option → left blank
    assert await page.evaluate("document.getElementById('q_certify').checked") is False
    assert await page.evaluate(
        "Array.from(document.querySelectorAll('input[type=file]')).map(i => i.files[0]?.name)"
    ) == ["Lovelace_resume.pdf", "Lovelace_cover_letter.pdf"]
    assert await page.evaluate("!!window.__submitted") is False


async def test_lever(page, applicant, documents) -> None:  # type: ignore[no-untyped-def]
    await load_fixture(page, "lever_form.html")
    filler = get_filler(ATSType.LEVER)
    fields = await filler.detect_fields(page)

    canon = {label: f.canonical for label, f in _by_label(fields).items()}
    assert canon["Full name"] == "full_name"
    assert canon["Current company"] == "current_company"
    assert canon["LinkedIn URL"] == "linkedin_url"
    assert canon["How did you hear about this job?"] == "referral_source"
    assert canon["Are you authorized to work in the US?"] == "work_authorization"
    assert canon["Gender"] == canon["Disability status"] == "_eeo"
    assert any(f.canonical == "resume" and f.input_type == "file" for f in fields)

    result = await filler.fill(page, applicant, fields, documents)
    assert len(result.filled) == 11
    assert len(result.flagged) == 3   # referral + work auth (medium), free-text textarea
    assert result.skipped_never == []

    values = await page.evaluate(FIELD_VALUES_JS)
    assert values["name"] == "Ada Lovelace"
    assert values["org"] == "Acme Corp"
    assert values["eeo[gender]"] == "Decline to self-identify"
    assert values["eeo[disability]"] == "I don't wish to answer"
    assert await page.evaluate(
        "document.querySelector('input[type=radio][value=Yes]').checked") is True
    assert await page.evaluate("!!window.__submitted") is False


async def test_prefilled_values_not_overwritten(page, applicant) -> None:  # type: ignore[no-untyped-def]
    await page.set_content("""
        <label for=e>Email</label><input id=e name=email value="other@example.com">
        <label for=p>Phone</label><input id=p name=phone value="(555) 0100">""")
    filler = get_filler(ATSType.INDEED_EASY)
    result = await filler.fill(page, applicant, await filler.detect_fields(page))
    assert await page.evaluate("document.getElementById('e').value") == "other@example.com"
    assert [f.label for f in result.flagged] == ["Email"]    # differs → flagged, kept
    assert [f.label for f in result.filled] == ["Phone"]     # same digits → already filled
