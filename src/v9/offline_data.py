"""Opt-in, pure fixture adapter and source-scoped coverage; no runtime wiring."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .contracts import Identity, Mapping, Observation, Window, digest, text, utc


@dataclass(frozen=True, slots=True)
class Stream:
    identity: Identity
    role: str
    source: str

    def __post_init__(self):
        if type(self.identity) is not Identity:
            raise TypeError("identity required")
        text(self.role)
        text(self.source)

    @property
    def business_key(self):
        return self.identity.business_key, self.role, self.source


@dataclass(frozen=True, slots=True)
class Coverage:
    stream: Stream
    window: Window
    status: str  # verified_complete, gap, unknown
    proof_kind: str  # independently_verified, last_n, snapshot, rollup, unknown
    evidence_ref: str
    available_at: datetime | None

    def __post_init__(self):
        if type(self.stream) is not Stream or type(self.window) is not Window:
            raise TypeError("immutable stream/window required")
        if self.status not in ("verified_complete", "gap", "unknown"):
            raise ValueError("invalid coverage status")
        if self.proof_kind not in (
            "independently_verified",
            "last_n",
            "snapshot",
            "rollup",
            "unknown",
        ):
            raise ValueError("invalid coverage proof")
        if self.status == "verified_complete" and self.proof_kind != "independently_verified":
            raise ValueError("source does not prove continuous coverage")
        text(self.evidence_ref)
        if self.available_at is not None:
            utc(self.available_at)
            if self.available_at < self.window.end:
                raise ValueError("coverage cannot precede window end")

    def for_window(self, stream: Stream, window: Window, cutoff: datetime) -> bool | None:
        utc(cutoff)
        if self.stream.business_key != stream.business_key or self.window != window:
            raise ValueError("coverage scope mismatch")
        if self.available_at is None or self.available_at > cutoff or self.status == "unknown":
            return None
        return self.status == "verified_complete"


@dataclass(frozen=True, slots=True)
class RawField:
    stream: Stream
    source_id: str
    input_ref: str
    field: str
    raw_value: Decimal | None
    raw_unit: str | None
    event_time: datetime
    received_at: datetime | None
    available_at: datetime | None
    unknown_reason: str | None
    side: str | None
    closed: bool
    mapping_version: str | None
    kind: str = "tick"
    candle_close_at: datetime | None = None

    def __post_init__(self):
        if type(self.stream) is not Stream:
            raise TypeError("stream required")
        for value in (self.source_id, self.input_ref, self.field):
            text(value)
        if self.raw_unit is not None:
            text(self.raw_unit)
        if self.mapping_version is not None:
            text(self.mapping_version)
        if self.side is not None:
            text(self.side)
        if self.raw_value is not None and (
            type(self.raw_value) is not Decimal or not self.raw_value.is_finite()
        ):
            raise ValueError("finite Decimal required")
        utc(self.event_time)
        for instant in (self.received_at, self.available_at):
            if instant is not None:
                utc(instant)
        if (self.received_at is None or self.available_at is None) and not self.unknown_reason:
            raise ValueError("unknown availability requires reason")
        if self.unknown_reason is not None:
            text(self.unknown_reason)
        if self.received_at is not None and self.received_at < self.event_time:
            raise ValueError("receipt before event")
        if self.available_at is not None and (
            self.received_at is None or self.available_at < self.received_at
        ):
            raise ValueError("availability before receipt")
        if type(self.closed) is not bool:
            raise TypeError("closed must be boolean")
        if self.kind not in ("tick", "candle"):
            raise ValueError("invalid record kind")
        if self.kind == "candle":
            if self.candle_close_at is None:
                raise ValueError("candle close time required")
            utc(self.candle_close_at)
            if self.candle_close_at <= self.event_time:
                raise ValueError("invalid candle close time")
        elif self.candle_close_at is not None:
            raise ValueError("tick cannot specify candle close")


@dataclass(frozen=True, slots=True)
class Adapted:
    raw: RawField
    observation: Observation | None
    exclusions: tuple[str, ...]
    diagnostics: tuple[str, ...]


def adapt(
    raw: RawField,
    *,
    window: Window,
    evaluated_at: datetime,
    declared_unit: str | None,
    declared_field: str | None,
    require_closed: bool,
    mapping: Mapping | None = None,
    enabled: bool = False,
) -> Adapted:
    """No guessed conversions or fallback OI fields. Explicit inputs only."""
    if enabled is not True:
        raise RuntimeError("offline adapter disabled")
    if type(raw) is not RawField or type(window) is not Window:
        raise TypeError("immutable inputs required")
    utc(evaluated_at)
    if type(require_closed) is not bool:
        raise TypeError("closed policy must be boolean")
    if declared_unit is not None:
        text(declared_unit)
    if declared_field is not None:
        text(declared_field)
    exclusions, diagnostics = [], []
    if not window.start <= raw.event_time < window.end:
        exclusions.append("EVENT_OUTSIDE_WINDOW")
    if raw.event_time > evaluated_at:
        exclusions.append("EVENT_AFTER_CUTOFF")
    if raw.received_at is None or raw.available_at is None:
        exclusions.append("AVAILABILITY_UNKNOWN")
    elif raw.available_at > evaluated_at or raw.received_at > evaluated_at:
        exclusions.append("AVAILABLE_AFTER_CUTOFF")
    if raw.kind == "candle":
        if raw.closed and raw.available_at is not None and raw.available_at < raw.candle_close_at:
            exclusions.append("CANDLE_AVAILABLE_BEFORE_CLOSE")
        if require_closed and (not raw.closed or raw.candle_close_at > evaluated_at):
            exclusions.append("CANDLE_OPEN")
    if raw.raw_unit is None or declared_unit is None or raw.raw_unit != declared_unit:
        diagnostics.append("UNIT_UNKNOWN_OR_MISMATCH")
    if declared_field is None or raw.field != declared_field:
        diagnostics.append("FIELD_SEMANTICS_UNSUPPORTED")
    if raw.mapping_version is not None:
        if (
            type(mapping) is not Mapping
            or mapping.version != raw.mapping_version
            or raw.stream.identity.business_key
            not in (mapping.spot.business_key, mapping.derivative.business_key)
            or not mapping.validity.start <= evaluated_at < mapping.validity.end
        ):
            diagnostics.append("MAPPING_UNVERIFIED")
    if raw.side not in ("buy", "sell"):
        diagnostics.append("SIDE_UNKNOWN")
    if exclusions:
        return Adapted(raw, None, tuple(exclusions), tuple(diagnostics))
    observation = Observation(
        raw.stream.identity,
        raw.stream.source,
        raw.input_ref,
        raw.raw_value if "FIELD_SEMANTICS_UNSUPPORTED" not in diagnostics else None,
        raw.raw_unit if "UNIT_UNKNOWN_OR_MISMATCH" not in diagnostics else None,
        raw.side if raw.side in ("buy", "sell") else None,
        raw.event_time,
        raw.received_at,
        raw.available_at,
        evaluated_at,
        window,
        raw.stream.role,
        raw.unknown_reason,
        raw.closed if raw.kind == "candle" else True,
    )
    return Adapted(raw, observation, (), tuple(diagnostics))


def semantic_fingerprint(raw: RawField) -> str:
    """Stable source-event semantics; receipt and capture references are not payload."""
    if type(raw) is not RawField:
        raise TypeError("raw field required")
    return digest(
        (
            raw.stream.business_key,
            raw.source_id,
            raw.field,
            raw.raw_value,
            raw.raw_unit,
            raw.event_time,
            raw.side,
            raw.closed if raw.kind == "candle" else None,
            raw.kind,
            raw.candle_close_at,
        )
    )


def capability(source_kind: str) -> tuple[str, str]:
    values = {
        "trades_last_n": ("UNKNOWN", "LAST_N_DOES_NOT_PROVE_COVERAGE"),
        "book_snapshot": ("SNAPSHOT", "NO_SEQUENCED_CONTINUOUS_BOOK"),
        "liquidations": ("UNSUPPORTED", "LIQUIDATION_COVERAGE_UNKNOWN"),
        "oi_ambiguous": ("UNSUPPORTED", "OI_FIELD_SEMANTICS_UNKNOWN"),
    }
    if source_kind not in values:
        raise ValueError("unknown source capability")
    return values[source_kind]


@dataclass(frozen=True, slots=True)
class Revision:
    stream: Stream
    window: Window
    version: str
    predecessor: str | None
    value: Decimal
    input_refs: tuple[str, ...]
    available_at: datetime

    def __post_init__(self):
        if type(self.stream) is not Stream or type(self.window) is not Window:
            raise TypeError("stream and window required")
        text(self.version)
        if self.predecessor is not None:
            text(self.predecessor)
        if type(self.value) is not Decimal or not self.value.is_finite():
            raise ValueError("finite value required")
        if type(self.input_refs) is not tuple or not self.input_refs:
            raise ValueError("immutable revision inputs required")
        for ref in self.input_refs:
            text(ref)
        utc(self.available_at)
        if self.available_at < self.window.end:
            raise ValueError("revision before window end")


def correct(
    original: Revision,
    *,
    coverage: Coverage,
    version: str,
    value: Decimal,
    input_refs: tuple[str, ...],
    input_available_at: tuple[datetime, ...],
    completed_at: datetime,
) -> Revision:
    utc(completed_at)
    if (
        type(input_refs) is not tuple
        or not input_refs
        or type(input_available_at) is not tuple
        or len(input_available_at) != len(input_refs)
    ):
        raise ValueError("INPUT_AVAILABILITY_REQUIRED")
    for instant in input_available_at:
        utc(instant)
        if instant > completed_at:
            raise ValueError("INPUT_UNAVAILABLE_AT_COMPLETION")
    if coverage.for_window(original.stream, original.window, completed_at) is not True:
        raise ValueError("COMPLETE_COVERAGE_REQUIRED")
    if completed_at <= original.available_at:
        raise ValueError("COMPLETION_BEFORE_PREDECESSOR")
    if version == original.version:
        raise ValueError("REVISION_VERSION_CONFLICT")
    return Revision(
        original.stream,
        original.window,
        version,
        original.version,
        value,
        input_refs + (coverage.evidence_ref,),
        max(original.available_at, coverage.available_at, completed_at),
    )


def revision_at(revisions: tuple[Revision, ...], cutoff: datetime) -> Revision | None:
    utc(cutoff)
    if type(revisions) is not tuple or any(type(x) is not Revision for x in revisions):
        raise TypeError("immutable revisions required")
    versions = {item.version: item for item in revisions}
    if len(versions) != len(revisions):
        raise ValueError("REVISION_VERSION_CONFLICT")
    if revisions and sum(item.predecessor is None for item in revisions) != 1:
        raise ValueError("AMBIGUOUS_REVISION_HISTORY")
    for item in revisions:
        if revisions and (
            item.stream.business_key != revisions[0].stream.business_key
            or item.window != revisions[0].window
        ):
            raise ValueError("REVISION_SCOPE_MISMATCH")
        if item.predecessor is not None:
            parent = versions.get(item.predecessor)
            if parent is None or parent.available_at >= item.available_at:
                raise ValueError("INVALID_REVISION_PREDECESSOR")
    children = [item.predecessor for item in revisions if item.predecessor is not None]
    if len(children) != len(set(children)):
        raise ValueError("AMBIGUOUS_REVISION_HISTORY")
    known = [x for x in revisions if x.available_at <= cutoff]
    if not known:
        return None
    return max(known, key=lambda x: (x.available_at, x.version, digest(x)))
