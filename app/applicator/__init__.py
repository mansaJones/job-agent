"""Applicator — apply queue, applicant data, and pre-flight checks (Jetson side).

The Windows apply client (apply_client/, phase 4b) consumes the queue over
the authenticated /api/apply-queue/* API. It fills forms but never submits.
"""
