"""Fixture quality evaluation with explicit, versioned inputs and provenance."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .contracts import InputContext, Observation, Quality, Status, Window, digest, text, utc


@dataclass(frozen=True, slots=True)
class FixturePolicy:
    name: str
    version: str
    require_side: bool
    require_unit: bool
    require_closed: bool
    max_age_seconds: int | None

    def __post_init__(self):
        text(self.name)
        text(self.version)
        if any(
            type(v) is not bool for v in (self.require_side, self.require_unit, self.require_closed)
        ):
            raise ValueError("POLICY_INVALID_FLAGS")
        if self.max_age_seconds is not None and (
            type(self.max_age_seconds) is not int or self.max_age_seconds < 0
        ):
            raise ValueError("POLICY_INVALID_MAX_AGE")


def assess(
    observations: tuple[Observation, ...],
    *,
    coverage: bool | None,
    context: InputContext,
    policy: FixturePolicy | None,
    evaluated_at: datetime,
    window: Window,
    evidence_refs: tuple[str, ...],
) -> Quality:
    if type(observations) is not tuple or any(not isinstance(o, Observation) for o in observations):
        raise TypeError("observations must be an immutable tuple")
    utc(evaluated_at)
    if type(context) is not InputContext:
        raise TypeError("CONTEXT_INVALID")
    if type(window) is not Window or window.end > evaluated_at:
        raise ValueError("analysis window must end by evaluation")
    if coverage is not None and type(coverage) is not bool:
        raise ValueError("coverage must be boolean or unknown")
    if policy is not None and type(policy) is not FixturePolicy:
        raise ValueError("POLICY_INVALID")
    if type(evidence_refs) is not tuple or not evidence_refs:
        raise ValueError("evidence references required")
    for ref in evidence_refs:
        text(ref)
    if any(o.evaluated_at != evaluated_at or o.window != window for o in observations):
        raise ValueError("observation evaluation/window mismatch")
    if any(
        o.identity.business_key != context.identity.business_key
        or o.input_role != context.role
        or o.unit != context.unit
        for o in observations
    ):
        raise ValueError("OBSERVATION_CONTEXT_MISMATCH")
    refs = tuple(o.input_ref for o in observations)

    def result(status, reason, activity=None):
        return Quality(
            context,
            status,
            reason,
            coverage,
            activity,
            refs,
            evaluated_at,
            window,
            policy.name if policy else None,
            policy.version if policy else None,
            digest(policy) if policy else None,
            evidence_refs,
        )

    if policy is None:
        return result(Status.INSUFFICIENT_HISTORY, "POLICY_MISSING")
    if coverage is None:
        return result(Status.MISSING, "COVERAGE_UNKNOWN")
    if not coverage:
        return result(Status.GAP, "COVERAGE_GAP")
    if policy.max_age_seconds is not None and evaluated_at - window.end > timedelta(
        seconds=policy.max_age_seconds
    ):
        return result(Status.STALE, "FRESHNESS_EXCEEDED")
    if not observations:
        if policy.require_unit and context.unit is None:
            return result(Status.UNSUPPORTED, "UNIT_UNKNOWN")
        return result(Status.VALID, "COVERED_ZERO_ACTIVITY", Decimal(0))
    if any(o.available_at is None or o.received_at is None for o in observations):
        return result(Status.UNSUPPORTED, "TIME_UNKNOWN")
    if policy.max_age_seconds is not None and any(
        evaluated_at - o.event_time > timedelta(seconds=policy.max_age_seconds)
        for o in observations
    ):
        return result(Status.STALE, "FRESHNESS_EXCEEDED")
    if policy.require_side and any(o.side is None for o in observations):
        return result(Status.UNSUPPORTED, "SIDE_UNKNOWN")
    if policy.require_unit and any(o.unit is None for o in observations):
        return result(Status.UNSUPPORTED, "UNIT_UNKNOWN")
    if policy.require_closed and any(
        not o.closed or o.window.end > evaluated_at for o in observations
    ):
        return result(Status.INSUFFICIENT_HISTORY, "CANDLE_OPEN")
    if any(o.value is None for o in observations):
        return result(Status.MISSING, "VALUE_MISSING")
    return result(Status.VALID, "VALID")
