"""Field matching tiers, never-fill precedence, and the action policy (no browser needed)."""

from __future__ import annotations

import pytest

from apply_client.ats.field_matcher import decide_action, match_field
from apply_client.models import Action, Confidence, FormField

H, M, L = Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW


@pytest.mark.parametrize("kwargs, expected", [
    # exact on any attribute → HIGH
    ({"label_text": "First Name *"}, ("first_name", H)),
    ({"label_text": "E-mail"}, ("email", H)),
    ({"label_text": "LinkedIn URL"}, ("linkedin_url", H)),
    ({"label_text": None, "aria_label": "Last name"}, ("last_name", H)),
    ({"label_text": "", "name_attr": "phone"}, ("phone", H)),
    # substring in label/aria → MEDIUM
    ({"label_text": "What is your current company?"}, ("current_company", M)),
    ({"label_text": "Are you legally authorized to work in the US?"}, ("work_authorization", M)),
    ({"label_text": "How did you hear about us?"}, ("referral_source", M)),
    # substring only in name/id/placeholder → LOW
    ({"label_text": "", "name_attr": "job_application[email]"}, ("email", L)),
    ({"label_text": "", "id_attr": "legalNameSection_firstName"}, ("first_name", L)),
    ({"label_text": "", "placeholder": "Enter your phone number here"}, ("phone", L)),
    # no match
    ({"label_text": "Anything else?"}, (None, L)),
    ({"label_text": "Username"}, (None, L)),  # whole words: "name" must not match "username"
])
def test_tiers(kwargs: dict, expected: tuple) -> None:
    assert match_field(**kwargs) == expected


@pytest.mark.parametrize("label, expected", [
    ("Type your full name as signature", "_never_signature"),
    ("Electronic Signature (type your name)", "_never_signature"),
    ("Social Security Number", "_never_ssn"),
    ("Date of Birth", "_never_dob"),
    ("Driver's License Number", "_never_gov_id"),
    ("I authorize a background check", "_never_consent"),
    ("I certify that my answers are true", "_never_consent"),
    ("Password", "_never_credentials"),
])
def test_never_fill_precedence(label: str, expected: str) -> None:
    assert match_field(label)[0] == expected


def test_authorized_to_work_is_not_consent() -> None:
    # "authorize" is a never-fill word; "authorized to work" must not trip it
    assert match_field("Are you authorized to work in the United States?")[0] == "work_authorization"


def test_salary_capped_at_medium() -> None:
    assert match_field("Salary") == ("salary_expectation", M)
    assert match_field("Desired salary") == ("salary_expectation", M)


@pytest.mark.parametrize("label", ["Gender", "Race", "Ethnicity", "Veteran status",
                                   "Disability", "Are you Hispanic or Latino?"])
def test_eeo_detection(label: str) -> None:
    assert match_field(label)[0] == "_eeo"


def _field(canonical, confidence, input_type="text"):  # type: ignore[no-untyped-def]
    return FormField(selector="#x", label="x", input_type=input_type,
                     canonical=canonical, confidence=confidence)


@pytest.mark.parametrize("canonical, confidence, input_type, action", [
    ("email", H, "text", Action.FILL),
    ("work_authorization", M, "select", Action.FILL_AND_FLAG),
    ("email", L, "text", Action.FLAG),
    (None, L, "text", Action.FLAG),
    ("_never_ssn", H, "text", Action.NEVER),
    (None, L, "password", Action.NEVER),
    ("_eeo", H, "select", Action.DECLINE),
    ("_eeo", H, "radio", Action.DECLINE),
    ("_eeo", H, "text", Action.FLAG),
    ("resume", H, "file", Action.UPLOAD),
    ("resume", H, "textarea", Action.FLAG),  # "paste your resume" — human's job
    (None, L, "file", Action.FLAG),
    ("referral_source", M, "textarea", Action.FLAG),
    ("currently_employed", H, "checkbox", Action.FLAG),  # never tick boxes
])
def test_decide_action(canonical, confidence, input_type, action) -> None:  # type: ignore[no-untyped-def]
    assert decide_action(_field(canonical, confidence, input_type)) == action
