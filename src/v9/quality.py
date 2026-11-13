"""Explicit quality checks; absence and zero are distinct."""

from decimal import Decimal

from .contracts import Observation, Quality, Status


def assess(
    observations: tuple[Observation, ...],
    *,
    coverage: bool | None,
    require_side: bool,
    require_unit: bool,
    require_closed: bool,
    policy_present: bool,
) -> Quality:
    if type(observations) is not tuple:
        raise TypeError("observations must be a tuple")
    refs = tuple(o.input_ref for o in observations)
    if not policy_present:
        return Quality(Status.INSUFFICIENT_HISTORY, "POLICY_MISSING", coverage, None, refs)
    if coverage is None:
        return Quality(Status.MISSING, "COVERAGE_UNKNOWN", None, None, refs)
    if not coverage:
        return Quality(Status.GAP, "COVERAGE_GAP", False, None, refs)
    if not observations:
        return Quality(Status.VALID, "COVERED_ZERO_ACTIVITY", True, Decimal(0))
    if any(o.available_at is None or o.received_at is None for o in observations):
        return Quality(Status.UNSUPPORTED, "TIME_UNKNOWN", True, None, refs)
    if require_side and any(o.side is None for o in observations):
        return Quality(Status.UNSUPPORTED, "SIDE_UNKNOWN", True, None, refs)
    if require_unit and any(o.unit is None for o in observations):
        return Quality(Status.UNSUPPORTED, "UNIT_UNKNOWN", True, None, refs)
    if require_closed and any(not o.closed for o in observations):
        return Quality(Status.INSUFFICIENT_HISTORY, "CANDLE_OPEN", True, None, refs)
    if any(o.value is None for o in observations):
        return Quality(Status.MISSING, "VALUE_MISSING", True, None, refs)
    return Quality(Status.VALID, "VALID", True, None, refs)
