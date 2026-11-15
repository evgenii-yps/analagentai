"""Read-only adapter over caller-supplied legacy instrument records."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping as Record
from types import MappingProxyType
from typing import Any

from .contracts import Identity, Mapping, MarketType, text


class Registry:
    def __init__(
        self,
        records: Iterable[Record[str, Any]],
        mappings: Iterable[Mapping],
        *,
        schema_version: str,
    ):
        entries = {}
        ids = set()
        symbols = set()
        for row in records:
            identifier = row["id"]
            if type(identifier) is not int or identifier <= 0:
                raise ValueError("registry id must be a positive integer")
            ident = Identity(
                row["exchange"],
                str(identifier),
                MarketType.SPOT
                if row["type"] == "spot"
                else MarketType.DERIVATIVE
                if row["type"] in ("swap", "future")
                else MarketType(row["type"]),
                row["quote"],
                schema_version,
            )
            symbol, base = row["symbol"], row["base"]
            text(symbol)
            text(base)
            key = ident.business_key
            symbol_key = (ident.venue, symbol, row["type"])
            if identifier in ids or symbol_key in symbols:
                raise ValueError("duplicate or conflicting registry id or source identity")
            ids.add(identifier)
            symbols.add(symbol_key)
            if key in entries:
                raise ValueError(f"duplicate identity: {key}")
            entries[key] = (ident, symbol, base)
        pairs = tuple(mappings)
        seen_spot, seen_derivative = set(), set()
        for pair in pairs:
            if type(pair) is not Mapping:
                raise TypeError("mappings must contain immutable Mapping values")
            s, d = pair.spot.business_key, pair.derivative.business_key
            if s not in entries or d not in entries:
                raise ValueError("mapping references unknown identity")
            if (
                entries[s][1] != pair.spot_source_identity
                or entries[d][1] != pair.derivative_source_identity
            ):
                raise ValueError("mapping source identity does not match registry symbol")
            if entries[s][2] != entries[d][2]:
                raise ValueError("mapping base mismatch")
            if s in seen_spot or d in seen_derivative:
                raise ValueError("ambiguous mapping")
            seen_spot.add(s)
            seen_derivative.add(d)
        self._entries = MappingProxyType(entries)
        self.mappings = pairs

    def resolve(self, identity: Identity) -> tuple[Identity, str, str] | None:
        return self._entries.get(identity.business_key)
