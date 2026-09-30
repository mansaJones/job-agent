"""Canonical field → known label variants.

Matched case-insensitively on whole words after normalization (punctuation
stripped, camelCase and snake_case split), so "firstName", "first_name" and
"First Name *" all read as "first name".

Canonicals starting with "_never" are matched so they can be skipped
explicitly; "_eeo" gets "decline to self-identify". These always win over
regular canonicals (a "signature" field that mentions "name" is a signature).
"""

from __future__ import annotations

SYNONYMS: dict[str, list[str]] = {
    "first_name": ["first name", "given name", "firstname", "legal first name", "preferred first name"],
    "last_name": ["last name", "surname", "family name", "lastname", "legal last name"],
    "full_name": ["full name", "name", "your name", "legal name"],
    "email": ["email", "e-mail", "email address"],
    "phone": ["phone", "telephone", "mobile", "phone number", "cell", "mobile phone"],
    "location": ["location", "current location", "where are you located", "address"],
    "city": ["city", "town"],
    "state": ["state", "province", "state province", "region"],
    "linkedin_url": ["linkedin", "linkedin profile", "linkedin url"],
    "portfolio_url": ["portfolio", "website", "personal website", "github", "url", "portfolio url"],
    "resume": ["resume", "cv", "resume/cv", "upload resume", "attach resume"],
    "cover_letter": ["cover letter", "letter of interest"],
    "current_company": ["current company", "current employer", "employer", "company"],
    "current_title": ["current title", "job title", "current position", "title"],
    "years_experience": ["years of experience", "total experience", "how many years"],
    "work_authorization": ["authorized to work", "work authorization", "legally authorized",
                           "eligible to work"],
    "sponsorship_needed": ["sponsorship", "require sponsorship", "visa sponsorship"],
    "start_availability": ["start date", "available to start", "availability", "notice period"],
    "willing_to_relocate": ["relocate", "relocation", "willing to relocate"],
    "remote_preference": ["remote", "work location preference", "hybrid"],
    "salary_expectation": ["salary", "compensation", "expected salary", "desired salary",
                           "pay expectations"],
    "referral_source": ["how did you hear", "referral source", "source", "where did you find"],
    "previously_applied": ["previously applied", "applied before", "worked here before"],
    "currently_employed": ["currently employed"],
    "ok_to_contact_employer": ["contact your current employer", "may we contact"],
    # NEVER-fill canonicals — matched so they can be skipped explicitly
    "_never_ssn": ["ssn", "social security"],
    "_never_dob": ["date of birth", "birth date", "birthday", "dob"],
    "_never_gov_id": ["driver's license", "drivers license", "passport", "government id",
                      "national id"],
    "_never_signature": ["signature", "sign", "type your name", "electronic signature",
                         "e-signature"],
    "_never_consent": ["consent", "authorize", "background check", "drug test", "certify",
                       "attest", "i agree", "acknowledge"],
    "_never_payment": ["credit card", "card number", "bank account", "routing number", "cvv"],
    "_never_credentials": ["password", "passcode", "verification code", "one time code"],
    "_eeo": ["gender", "race", "ethnicity", "veteran", "disability", "sexual orientation",
             "hispanic", "latino", "pronouns"],
}
