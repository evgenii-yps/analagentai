"""Repeatable synthetic fixture benchmark. Requires two explicitly named local test DBs."""

import json
import os
import statistics
import time
from dataclasses import replace
from decimal import Decimal

from fixture_targets import connect_targets
from test_durable import NOW, POLICY, WINDOW, profile

from src.v9.contracts import Identity, MarketType, digest
from src.v9.durable import PostgresPublisher, archive, dumps, initialize, put_evidence, restore
from src.v9.offline_data import Coverage, RawField, Stream
from src.v9.offline_replay import sample_replay


def run():
    records = []
    fixtures = []
    for n in range(5):
        ident = Identity("fixture", str(n), MarketType.SPOT, "USD", "1")
        stream = Stream(ident, "trade", "fixture")
        cov = Coverage(
            stream, WINDOW, "verified_complete", "independently_verified", f"proof-{n}", NOW
        )
        rows = tuple(
            RawField(
                stream,
                str(i),
                f"{n}-{i}",
                "amount",
                Decimal(i) / 100,
                "base",
                WINDOW.start,
                WINDOW.start,
                NOW,
                None,
                "buy",
                True,
                None,
            )
            for i in range(200)
        )
        records.extend(rows)
        fixtures.append((stream, cov, rows))
    input_bytes = sum(len(dumps(row)) for row in records)
    with connect_targets(os.environ.get("STAGE1B2_SOURCE"), os.environ.get("STAGE1B2_RESTORE")) as (
        a,
        b,
        instance,
    ):
        initialize(a)
        start = time.perf_counter()
        for record in records:
            put_evidence(a, "raw", record.input_ref, record)
        ingest = time.perf_counter() - start
        start = time.perf_counter()
        for n in range(5):
            # Rebind nested identity to keep all profile provenance internally consistent.
            base = profile(str(n))
            ident = fixtures[n][0].identity
            measurement = base.measurements[0]
            quality = replace(
                measurement.quality, context=replace(measurement.quality.context, identity=ident)
            )
            measurement = replace(measurement, quality=quality, additional_quality=(quality,))
            p = replace(base, identity=ident, measurements=(measurement,), mappings=())
            PostgresPublisher(a).publish(p, 0)
        publication = time.perf_counter() - start
        start = time.perf_counter()
        manifests = []
        for stream, cov, rows in fixtures:
            manifest, output = sample_replay(
                stream=stream,
                window=WINDOW,
                coverage=cov,
                records=rows,
                cutoff=NOW,
                generated_at=NOW,
                schema_version="1",
                calculation_version="fixture-v1",
                policy=POLICY,
                unit="base",
                field="amount",
                require_closed=True,
            )
            manifests.append((manifest, output))
        replay_s = time.perf_counter() - start
        start = time.perf_counter()
        data = archive(a)
        archive_s = time.perf_counter() - start
        start = time.perf_counter()
        restore(b, data)
        restore_s = time.perf_counter() - start
        assert archive(b) == data
        db_bytes = a.execute(
            "SELECT sum(pg_total_relation_size(c.oid)) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='stage1b2' AND c.relkind IN ('r','m')"
        ).fetchone()[0]
    return dict(
        fixture_instance_id=instance.container_id,
        instruments=5,
        rows=len(records),
        input_bytes=input_bytes,
        ingest_s=ingest,
        publication_s=publication,
        replay_s=replay_s,
        archive_s=archive_s,
        restore_s=restore_s,
        db_bytes=int(db_bytes),
        archive_bytes=len(data),
        output_digests=[digest(out) for _, out in manifests],
        manifest_digests=[m.content_digest for m, _ in manifests],
    )


if __name__ == "__main__":
    results = [run() for _ in range(3)]
    print(
        json.dumps(
            {
                "runs": results,
                "median_seconds": {
                    k: statistics.median(r[k] for r in results)
                    for k in ("ingest_s", "publication_s", "replay_s", "archive_s", "restore_s")
                },
            },
            indent=2,
        )
    )
