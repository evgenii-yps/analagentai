"""Fixture-only profile packaging and abstract publication contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .contracts import Identity, Quality, Status, Window, digest, text, utc


@dataclass(frozen=True, slots=True)
class Measurement:
    name: str
    value: Decimal
    window: Window
    quality: Quality
    source_refs: tuple[str, ...]
    available_at: datetime
    computed_at: datetime
    input_available_at: tuple[datetime, ...]
    confirmation: str

    def __post_init__(self):
        text(self.name)
        text(self.confirmation)
        if not isinstance(self.value, Decimal) or not self.value.is_finite():
            raise ValueError("finite Decimal required")
        if type(self.source_refs) is not tuple or not self.source_refs:
            raise ValueError("immutable source references required")
        for item in self.source_refs:
            text(item)
        utc(self.available_at)
        utc(self.computed_at)
        if type(self.input_available_at) is not tuple or not self.input_available_at:
            raise ValueError("input availability required")
        for instant in self.input_available_at:
            utc(instant)
        if self.available_at < max(self.computed_at, *self.input_available_at):
            raise ValueError("availability before computation")


@dataclass(frozen=True, slots=True)
class Profile:
    identity: Identity
    mapping_ref: str
    schema_version: str
    policy_version: str
    profile_version: str
    calculation_version: str
    data_cutoff: datetime
    evaluated_at: datetime
    applicable_from: datetime
    expires_at: datetime
    sufficiency: str
    provenance: tuple[str, ...]
    measurements: tuple[Measurement, ...]

    def __post_init__(self):
        for item in (
            self.mapping_ref,
            self.schema_version,
            self.policy_version,
            self.profile_version,
            self.calculation_version,
            self.sufficiency,
        ):
            text(item)
        if (
            type(self.provenance) is not tuple
            or not self.provenance
            or type(self.measurements) is not tuple
            or not self.measurements
        ):
            raise ValueError("immutable provenance and measurements required")
        for item in self.provenance:
            text(item)
        for item in (self.data_cutoff, self.evaluated_at, self.applicable_from, self.expires_at):
            utc(item)
        if not self.data_cutoff <= self.evaluated_at <= self.applicable_from < self.expires_at:
            raise ValueError("invalid profile timeline")
        if any(
            m.available_at > self.evaluated_at or m.window.end > self.data_cutoff
            for m in self.measurements
        ):
            raise ValueError("future measurement")
        if len({m.name for m in self.measurements}) != len(self.measurements):
            raise ValueError("duplicate measurement")

    @property
    def content_digest(self) -> str:
        return digest(self)


def build_profile(*, enabled: bool = False, **fields) -> Profile:
    if enabled is not True:
        raise RuntimeError("fixture builder disabled")
    profile = Profile(**fields)
    if any(m.quality.status != Status.VALID for m in profile.measurements):
        raise ValueError("supplied profile measurement is not valid")
    return profile


class Publisher(Protocol):
    """CAS: unique versions, no overwrite; failure leaves current pointer unchanged.

    Implementations must retain readable immutable prior versions. Durability and
    atomicity are explicitly deferred to Stage 1B.
    """

    def publish(self, profile: Profile, expected_generation: int) -> int: ...
    def read_version(self, version: str) -> Profile | None: ...
