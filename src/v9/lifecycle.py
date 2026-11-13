"""Pure lifecycle transitions; READY never conveys authorization."""

from dataclasses import dataclass
from datetime import datetime

from .contracts import Lifecycle, text, utc

_ORDER = (
    Lifecycle.DISCOVERED,
    Lifecycle.DATA_CHECK,
    Lifecycle.WARMUP,
    Lifecycle.SHADOW_VALIDATION,
    Lifecycle.READY,
)


@dataclass(frozen=True, slots=True)
class Evidence:
    version: str
    passed: frozenset[Lifecycle]
    mandatory_ok: bool
    unexpired: bool
    recovery_checked: bool = False

    def __post_init__(self):
        text(self.version)
        if type(self.passed) is not frozenset or not self.passed <= frozenset(_ORDER):
            raise ValueError("passed must be a frozenset of lifecycle states")


@dataclass(frozen=True, slots=True)
class Result:
    state: Lifecycle
    reason: str
    evidence_version: str
    grants_authorization: bool = False


def transition(
    current: Lifecycle, target: Lifecycle, evidence: Evidence, evaluated_at: datetime
) -> Result:
    utc(evaluated_at)
    if not isinstance(current, Lifecycle) or not isinstance(target, Lifecycle):
        raise ValueError("invalid lifecycle")
    if not evidence.mandatory_ok or not evidence.unexpired:
        return Result(Lifecycle.SUSPENDED, "MANDATORY_FAILURE_OR_EXPIRY", evidence.version)
    if target == Lifecycle.SUSPENDED:
        return Result(target, "EXPLICIT_SUSPENSION", evidence.version)
    if current == Lifecycle.SUSPENDED:
        if not evidence.recovery_checked or target == Lifecycle.READY:
            return Result(current, "RECOVERY_REQUIRED", evidence.version)
        if target not in evidence.passed or any(
            x not in evidence.passed for x in _ORDER[: _ORDER.index(target) + 1]
        ):
            return Result(current, "INSUFFICIENT_RECOVERY_EVIDENCE", evidence.version)
        return Result(target, "RECOVERED", evidence.version)
    if current == Lifecycle.READY or _ORDER.index(target) != _ORDER.index(current) + 1:
        return Result(current, "INVALID_TRANSITION", evidence.version)
    required = _ORDER[: _ORDER.index(target) + 1]
    if any(x not in evidence.passed for x in required):
        return Result(current, "EVIDENCE_MISSING", evidence.version)
    return Result(target, "ADVANCED", evidence.version)
