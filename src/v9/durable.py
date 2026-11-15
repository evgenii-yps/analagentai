"""Opt-in fixture persistence. Connections and disposable databases are caller-owned."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .contracts import (
    Identity,
    InputContext,
    Lifecycle,
    Mapping,
    MarketType,
    Quality,
    Status,
    Window,
)
from .offline_data import Coverage, RawField, Revision, Stream
from .offline_replay import Checkpoint, Manifest, MappingRequirement, UnresolvedConflict
from .profiles import Measurement, Profile
from .quality import FixturePolicy

# Explicit allowlist; decoding never imports or executes types from an archive.
_TYPES = {
    x.__name__: x
    for x in (
        Identity,
        InputContext,
        Mapping,
        Quality,
        Window,
        Coverage,
        RawField,
        Revision,
        Stream,
        Checkpoint,
        Manifest,
        MappingRequirement,
        UnresolvedConflict,
        Measurement,
        Profile,
        FixturePolicy,
        MarketType,
        Status,
        Lifecycle,
    )
}


def _pack(value):
    if isinstance(value, Enum):
        return {"@": type(value).__name__, "value": value.value}
    if isinstance(value, datetime):
        return {"@": "datetime", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"@": "decimal", "value": str(value)}
    if is_dataclass(value) and type(value).__name__ in _TYPES:
        return {
            "@": type(value).__name__,
            "fields": {f.name: _pack(getattr(value, f.name)) for f in fields(value)},
        }
    if type(value) is tuple:
        return {"@": "tuple", "items": [_pack(x) for x in value]}
    if value is None or type(value) in (str, int, bool):
        return value
    raise TypeError(f"unsupported value {type(value)}")


def _unpack(value):
    if not isinstance(value, dict):
        if value is None or type(value) in (str, int, bool):
            return value
        raise ValueError("invalid encoded value")
    tag = value.get("@")
    if tag == "datetime" and set(value) == {"@", "value"}:
        return datetime.fromisoformat(value["value"])
    if tag == "decimal" and set(value) == {"@", "value"}:
        return Decimal(value["value"])
    if tag == "tuple" and set(value) == {"@", "items"}:
        return tuple(_unpack(x) for x in value["items"])
    cls = _TYPES.get(tag)
    if cls is None:
        raise ValueError("unknown serialization type")
    if issubclass(cls, Enum) and set(value) == {"@", "value"}:
        return cls(value["value"])
    if set(value) != {"@", "fields"} or set(value["fields"]) != {f.name for f in fields(cls)}:
        raise ValueError("invalid serialized fields")
    return cls(**{k: _unpack(v) for k, v in value["fields"].items()})


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def dumps(value):
    return _json({"format": "fixture-object-v1", "object": _pack(value)})


def loads(payload):
    raw = json.loads(payload)
    if set(raw) != {"format", "object"} or raw["format"] != "fixture-object-v1":
        raise ValueError("unsupported object format")
    return _unpack(raw["object"])


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


DDL = """
CREATE SCHEMA stage1b2;
CREATE TABLE stage1b2.versions (
 version text PRIMARY KEY, business_key text NOT NULL, payload bytea NOT NULL,
 content_digest text NOT NULL, payload_sha text NOT NULL);
CREATE FUNCTION stage1b2.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable fixture history'; END $$;
CREATE TRIGGER immutable_versions BEFORE UPDATE OR DELETE ON stage1b2.versions
 FOR EACH ROW EXECUTE FUNCTION stage1b2.reject_mutation();
CREATE TABLE stage1b2.current_profiles (
 business_key text PRIMARY KEY, generation bigint NOT NULL CHECK(generation > 0),
 version text NOT NULL REFERENCES stage1b2.versions(version));
CREATE TABLE stage1b2.evidence (
 kind text NOT NULL, item_key text NOT NULL, payload bytea NOT NULL, payload_sha text NOT NULL,
 PRIMARY KEY(kind, item_key));
CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON stage1b2.evidence
 FOR EACH ROW EXECUTE FUNCTION stage1b2.reject_mutation();
"""


class StaleGeneration(ValueError):
    pass


def _require_idle(conn):
    if not conn.autocommit:
        raise ValueError("autocommit connection required")
    # libpq transaction status 0 is IDLE; INTRANS, INERROR, ACTIVE and UNKNOWN fail closed.
    if conn.info.transaction_status != 0:
        raise ValueError("idle connection required")


def initialize(conn):
    """Initialize a new, caller-selected disposable database; fails if schema exists."""
    _require_idle(conn)
    with conn.transaction():
        conn.execute(DDL)


def business_key(profile):
    return _json(list(profile.identity.business_key)).decode()


class PostgresPublisher:
    def __init__(self, conn, *, after_insert=None):
        if not conn.autocommit:
            raise ValueError("autocommit connection required")
        self.conn = conn
        self.after_insert = after_insert

    def publish(self, profile: Profile, expected_generation: int) -> int:
        if (
            type(profile) is not Profile
            or type(expected_generation) is not int
            or expected_generation < 0
        ):
            raise ValueError("invalid profile or generation")
        _require_idle(self.conn)
        key = business_key(profile)
        payload = dumps(profile)
        with self.conn.transaction():
            # Transaction-scoped lock serializes first publication as well as updates.
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
            row = self.conn.execute(
                "SELECT generation FROM stage1b2.current_profiles WHERE business_key=%s FOR UPDATE",
                (key,),
            ).fetchone()
            actual = row[0] if row else 0
            if actual != expected_generation:
                raise StaleGeneration(f"expected {expected_generation}; current {actual}")
            self.conn.execute(
                "INSERT INTO stage1b2.versions VALUES (%s,%s,%s,%s,%s)",
                (profile.profile_version, key, payload, profile.content_digest, sha(payload)),
            )
            if self.after_insert is not None:
                self.after_insert()
            self.conn.execute(
                "INSERT INTO stage1b2.current_profiles VALUES (%s,%s,%s) "
                "ON CONFLICT (business_key) DO UPDATE SET "
                "generation=EXCLUDED.generation,version=EXCLUDED.version",
                (key, actual + 1, profile.profile_version),
            )
        return actual + 1

    def read_version(self, version: str) -> Profile | None:
        row = self.conn.execute(
            "SELECT payload,content_digest,payload_sha FROM stage1b2.versions WHERE version=%s",
            (version,),
        ).fetchone()
        if row is None:
            return None
        payload = bytes(row[0])
        profile = loads(payload)
        if (
            type(profile) is not Profile
            or sha(payload) != row[2]
            or profile.content_digest != row[1]
        ):
            raise ValueError("corrupt profile")
        return profile

    def read_current(self, identity):
        key = _json(list(identity.business_key)).decode()
        row = self.conn.execute(
            "SELECT generation,version FROM stage1b2.current_profiles WHERE business_key=%s", (key,)
        ).fetchone()
        return None if row is None else (row[0], self.read_version(row[1]))


_EVIDENCE_TYPES = {
    "raw": RawField,
    "revision": Revision,
    "manifest": Manifest,
    "checkpoint": Checkpoint,
    "coverage": Coverage,
    "policy": FixturePolicy,
}


def put_evidence(conn, kind, key, value):
    _require_idle(conn)
    if kind not in _EVIDENCE_TYPES or type(value) is not _EVIDENCE_TYPES[kind] or not key:
        raise ValueError("invalid evidence")
    if kind == "checkpoint" and (
        value.integrity_format != "conflict-v1" or value.fingerprint_format != "semantic-v1"
    ):
        raise ValueError("unsupported checkpoint format")
    payload = dumps(value)
    with conn.transaction():
        conn.execute(
            "INSERT INTO stage1b2.evidence VALUES (%s,%s,%s,%s)", (kind, key, payload, sha(payload))
        )


def get_evidence(conn, kind, key):
    row = conn.execute(
        "SELECT payload,payload_sha FROM stage1b2.evidence WHERE kind=%s AND item_key=%s",
        (kind, key),
    ).fetchone()
    if row is None:
        return None
    payload = bytes(row[0])
    value = loads(payload)
    if sha(payload) != row[1] or type(value) is not _EVIDENCE_TYPES.get(kind):
        raise ValueError("corrupt evidence")
    return value


def archive(conn):
    """Consistent snapshot: payloads encoded as UTF-8 JSON, sorted primary keys."""
    _require_idle(conn)
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        versions = conn.execute(
            "SELECT version,business_key,payload,content_digest,payload_sha "
            "FROM stage1b2.versions ORDER BY version"
        ).fetchall()
        current = conn.execute(
            "SELECT business_key,generation,version FROM stage1b2.current_profiles "
            "ORDER BY business_key"
        ).fetchall()
        evidence = conn.execute(
            "SELECT kind,item_key,payload,payload_sha FROM stage1b2.evidence ORDER BY kind,item_key"
        ).fetchall()
    versions = [[a, b, bytes(c).decode(), d, e] for a, b, c, d, e in versions]
    evidence = [[a, b, bytes(c).decode(), d] for a, b, c, d in evidence]
    data = {
        "format": "stage1b2-archive-v1",
        "versions": versions,
        "current": current,
        "evidence": evidence,
    }
    body = _json(data)
    return _json({"sha256": sha(body), "data": data})


def restore(conn, payload):
    """Validate before writes, then create and populate a fresh schema atomically."""
    _require_idle(conn)
    outer = json.loads(payload)
    if set(outer) != {"sha256", "data"} or sha(_json(outer["data"])) != outer["sha256"]:
        raise ValueError("archive checksum mismatch")
    data = outer["data"]
    if (
        set(data) != {"format", "versions", "current", "evidence"}
        or data["format"] != "stage1b2-archive-v1"
    ):
        raise ValueError("unsupported archive")
    for version, key, text_payload, digest, checksum in data["versions"]:
        raw = text_payload.encode()
        obj = loads(raw)
        if (
            type(obj) is not Profile
            or obj.profile_version != version
            or business_key(obj) != key
            or obj.content_digest != digest
            or sha(raw) != checksum
        ):
            raise ValueError("invalid archived profile")
    for kind, key, text_payload, checksum in data["evidence"]:
        raw = text_payload.encode()
        obj = loads(raw)
        if not key or type(obj) is not _EVIDENCE_TYPES.get(kind) or sha(raw) != checksum:
            raise ValueError("invalid archived evidence")
        if kind == "checkpoint" and (
            obj.integrity_format != "conflict-v1" or obj.fingerprint_format != "semantic-v1"
        ):
            raise ValueError("unsupported checkpoint")
    versions_by_id = {r[0]: r for r in data["versions"]}
    if len(versions_by_id) != len(data["versions"]):
        raise ValueError("duplicate archived version")
    counts = {}
    for _, key, *_ in data["versions"]:
        counts[key] = counts.get(key, 0) + 1
    pointers = set()
    for key, generation, version in data["current"]:
        if (
            type(generation) is not int
            or generation < 1
            or version not in versions_by_id
            or versions_by_id[version][1] != key
            or key in pointers
            or generation != counts.get(key)
        ):
            raise ValueError("invalid current pointer")
        pointers.add(key)
    if pointers != set(counts):
        raise ValueError("missing current pointer")
    with conn.transaction():
        conn.execute(DDL)
        for version, key, text_payload, digest, checksum in data["versions"]:
            conn.execute(
                "INSERT INTO stage1b2.versions VALUES (%s,%s,%s,%s,%s)",
                (version, key, text_payload.encode(), digest, checksum),
            )
        for row in data["current"]:
            conn.execute("INSERT INTO stage1b2.current_profiles VALUES (%s,%s,%s)", row)
        for kind, key, text_payload, checksum in data["evidence"]:
            conn.execute(
                "INSERT INTO stage1b2.evidence VALUES (%s,%s,%s,%s)",
                (kind, key, text_payload.encode(), checksum),
            )
