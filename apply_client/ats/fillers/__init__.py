"""Per-ATS fillers. `get_filler(ats)` picks one; anything without a dedicated filler gets generic."""

from __future__ import annotations

from apply_client.ats.fillers.base import BaseFiller
from apply_client.ats.fillers.generic import GenericFiller
from apply_client.ats.fillers.greenhouse import GreenhouseFiller
from apply_client.ats.fillers.indeed import IndeedFiller
from apply_client.ats.fillers.lever import LeverFiller
from apply_client.ats.fillers.workday import WorkdayFiller
from apply_client.models import ATSType

_FILLERS: dict[ATSType, type[BaseFiller]] = {
    ATSType.GREENHOUSE: GreenhouseFiller,
    ATSType.LEVER: LeverFiller,
    ATSType.INDEED_EASY: IndeedFiller,
    ATSType.WORKDAY: WorkdayFiller,
}


def get_filler(ats: ATSType) -> BaseFiller:
    return _FILLERS.get(ats, GenericFiller)()
