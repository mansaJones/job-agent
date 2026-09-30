"""Generic filler — the base filler with no overrides.

Used for UNKNOWN pages and ATSs without a dedicated filler (iCIMS, Taleo,
SmartRecruiters, Ashby, BambooHR). Still handles name/email/phone/resume on
most custom career pages.
"""

from __future__ import annotations

from apply_client.ats.fillers.base import BaseFiller
from apply_client.models import ATSType


class GenericFiller(BaseFiller):
    ats = ATSType.UNKNOWN
