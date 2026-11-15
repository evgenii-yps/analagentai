"""Immutable fixture contracts; no runtime integration or clock access."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def text(value: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("nonempty canonical text required")


def utc(value: datetime) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError("UTC-aware datetime required")


def canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        utc(value)
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal")
        if value == 0:
            return "0"
        sign, digits, exponent = value.as_tuple()
        digits = list(digits)
        while len(digits) > 1 and digits[-1] == 0:
            digits.pop()
            exponent += 1
        return format(Decimal((sign, tuple(digits), exponent)), "f")
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: canonical(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [canonical(v) for v in value]
    if value is None or isinstance(value, str | bool | int):
        return value
    raise TypeError(f"unsupported canonical value {type(value).__name__}")


def encode(value: Any) -> bytes:
    return json.dumps(
        canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


class MarketType(str, Enum):
    SPOT = "spot"
    DERIVATIVE = "derivative"


class Status(str, Enum):
    VALID = "VALID"
    MISSING = "MISSING"
    STALE = "STALE"
    GAP = "GAP"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    UNSUPPORTED = "UNSUPPORTED"


class Lifecycle(str, Enum):
    DISCOVERED = "DISCOVERED"
    DATA_CHECK = "DATA_CHECK"
    WARMUP = "WARMUP"
    SHADOW_VALIDATION = "SHADOW_VALIDATION"
    READY = "READY"
    SUSPENDED = "SUSPENDED"


@dataclass(frozen=True, slots=True)
class Identity:
    venue: str
    instrument_id: str
    market_type: MarketType
    quote_currency: str
    schema_version: str

    def __post_init__(self) -> None:
        for v in (self.venue, self.instrument_id, self.quote_currency, self.schema_version):
            text(v)
        if not isinstance(self.market_type, MarketType):
            raise ValueError("invalid market type")

    @property
    def business_key(self) -> tuple[str, str, MarketType, str]:
        return self.venue, self.instrument_id, self.market_type, self.quote_currency


@dataclass(frozen=True, slots=True)
class Window:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        utc(self.start)
        utc(self.end)
        if self.start >= self.end:
            raise ValueError("window must be nonempty [start,end)")


@dataclass(frozen=True, slots=True)
class Mapping:
    spot: Identity
    derivative: Identity
    version: str
    provenance: str
    validity: Window
    spot_source_identity: str
    derivative_source_identity: str

    def __post_init__(self) -> None:
        if type(self.spot) is not Identity or type(self.derivative) is not Identity:
            raise TypeError("mapping identities must be immutable Identity values")
        if type(self.validity) is not Window:
            raise TypeError("mapping validity must be an immutable Window")
        for v in (
            self.version,
            self.provenance,
            self.spot_source_identity,
            self.derivative_source_identity,
        ):
            text(v)
        if (
            self.spot.market_type != MarketType.SPOT
            or self.derivative.market_type != MarketType.DERIVATIVE
        ):
            raise ValueError("mapping requires spot and derivative")
        if (
            self.spot.venue != self.derivative.venue
            or self.spot.quote_currency != self.derivative.quote_currency
        ):
            raise ValueError("mapping venue/quote mismatch")


@dataclass(frozen=True, slots=True)
class Observation:
    identity: Identity
    source: str
    input_ref: str
    value: Decimal | None
    unit: str | None
    side: str | None
    event_time: datetime
    received_at: datetime | None
    available_at: datetime | None
    evaluated_at: datetime
    window: Window
    input_role: str
    unknown_reason: str | None = None
    closed: bool = True

    def __post_init__(self) -> None:
        if type(self.identity) is not Identity or type(self.window) is not Window:
            raise TypeError("observation identity/window must be immutable contracts")
        text(self.input_role)
        text(self.source)
        text(self.input_ref)
        utc(self.event_time)
        utc(self.evaluated_at)
        if self.value is not None and (
            not isinstance(self.value, Decimal) or not self.value.is_finite()
        ):
            raise ValueError("finite decimal required")
        if self.unit is not None:
            text(self.unit)
        if self.side not in (None, "buy", "sell"):
            raise ValueError("unsupported side")
        if not self.window.start <= self.event_time < self.window.end:
            raise ValueError("event outside window")
        for t in (self.received_at, self.available_at):
            if t is not None:
                utc(t)
        if (self.received_at is None or self.available_at is None) and not self.unknown_reason:
            raise ValueError("unknown timestamp requires reason")
        if self.unknown_reason is not None:
            text(self.unknown_reason)
        if type(self.closed) is not bool:
            raise ValueError("closed must be boolean")
        if self.event_time > self.evaluated_at:
            raise ValueError("event after evaluation")
        if self.available_at is not None and self.received_at is None:
            raise ValueError("availability requires known receipt")
        if self.received_at is not None and self.received_at > self.evaluated_at:
            raise ValueError("receipt after evaluation")
        if self.received_at is not None and self.received_at < self.event_time:
            raise ValueError("receipt precedes event")
        if (
            self.available_at is not None
            and self.received_at is not None
            and self.available_at < self.received_at
        ):
            raise ValueError("availability precedes receipt")
        if self.available_at is not None and self.available_at > self.evaluated_at:
            raise ValueError("input unavailable at evaluation")


@dataclass(frozen=True, slots=True)
class InputContext:
    identity: Identity
    role: str
    unit: str | None

    def __post_init__(self) -> None:
        if type(self.identity) is not Identity:
            raise TypeError("context identity must be immutable Identity")
        text(self.role)
        if self.unit is not None:
            text(self.unit)


@dataclass(frozen=True, slots=True)
class Quality:
    context: InputContext
    status: Status
    reason: str
    coverage: bool | None
    activity: Decimal | None
    input_refs: tuple[str, ...]
    evaluated_at: datetime
    window: Window
    policy_name: str | None
    policy_version: str | None
    policy_digest: str | None
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.context) is not InputContext:
            raise TypeError("quality context must be immutable InputContext")
        utc(self.evaluated_at)
        if type(self.window) is not Window or self.window.end > self.evaluated_at:
            raise ValueError("quality window must end by evaluation")
        if (
            not (self.policy_name is None)
            == (self.policy_version is None)
            == (self.policy_digest is None)
        ):
            raise ValueError("policy identity and version must both be present or unknown")
        if self.policy_name is not None:
            text(self.policy_name)
            text(self.policy_version)
            text(self.policy_digest)
        if type(self.evidence_refs) is not tuple or not self.evidence_refs:
            raise ValueError("quality evidence references required")
        for ref in self.evidence_refs:
            text(ref)
        if not isinstance(self.status, Status):
            raise ValueError("invalid quality status")
        text(self.reason)
        if type(self.input_refs) is not tuple:
            raise TypeError("input_refs must be tuple")
        for ref in self.input_refs:
            text(ref)
        if self.coverage is not None and type(self.coverage) is not bool:
            raise ValueError("coverage must be boolean or unknown")
        if self.activity is not None and (
            not isinstance(self.activity, Decimal)
            or not self.activity.is_finite()
            or self.activity < 0
        ):
            raise ValueError("activity must be nonnegative finite decimal")
        if self.activity == 0 and self.coverage is not True:
            raise ValueError("zero requires coverage evidence")
        if self.status == Status.MISSING and self.activity is not None:
            raise ValueError("missing cannot carry activity")
