"""Scheduler: lease reaper, HITL digests (10:00/16:00), monthly partitions, retention, PHASE_3 analytics."""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date, datetime, timedelta

from . import hitl, infra, orchestrator

RETENTION_MONTHS = int(os.environ.get("NODE_EXEC_RETENTION_MONTHS", "6"))


def month_start(d: date, add: int = 0) -> date:
    y, m = divmod(d.month - 1 + add, 12)
    return date(d.year + y, m + 1, 1)


def ensure_partitions(c, today: date) -> None:
    for k in (0, 1, 2):
        s, e = month_start(today, k), month_start(today, k + 1)
        c.execute(f"CREATE TABLE IF NOT EXISTS node_executions_{s:%Y_%m} PARTITION OF node_executions "
                  f"FOR VALUES FROM ('{s}') TO ('{e}')")


def expire_partitions(today: date) -> list[str]:
    """DETACH CONCURRENTLY (must be outside a tx) -> export Parquet -> drop."""
    import psycopg
    cutoff = month_start(today, -RETENTION_MONTHS)
    done = []
    with psycopg.connect(infra.env("PG_DIRECT_DSN", infra.env("PG_DSN")), autocommit=True) as c:
        rows = c.execute("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
                         "JOIN pg_class p ON p.oid=i.inhparent WHERE p.relname='node_executions' "
                         "AND c.relname ~ '^node_executions_[0-9]{4}_[0-9]{2}$'").fetchall()
        for (name,) in rows:
            y, m = int(name[-7:-3]), int(name[-2:])
            if date(y, m, 1) >= cutoff:
                continue
            c.execute(f"ALTER TABLE node_executions DETACH PARTITION {name} CONCURRENTLY")
            if os.environ.get("ANALYTICS_ENABLED") == "1":
                _archive(c, name)
            c.execute(f"DROP TABLE {name}")
            done.append(name)
    return done


def _archive(conn, table: str) -> None:
    with tempfile.TemporaryDirectory() as d:
        out = f"{d}/{table}.parquet"
        cur = conn.execute(f"SELECT * FROM {table}")
        cols = [x.name for x in cur.description]
        rows = cur.fetchall()
        import pyarrow as pa
        import pyarrow.parquet as pq
        pq.write_table(pa.Table.from_pylist([dict(zip(cols, map(_plain, r))) for r in rows]), out)
        with open(out, "rb") as f:
            infra.s3().put_object(Bucket=infra.env("S3_BUCKET", "agdd"), Key=f"archive/node_executions/{table}.parquet",
                                  Body=f.read())


def _plain(v):
    return float(v) if v.__class__.__name__ == "Decimal" else (str(v) if v.__class__.__name__ == "UUID" else v)


def build_digest(c, at: datetime) -> int:
    bid = hitl.digest_batch_id(at)
    rows = c.execute("SELECT decision_id, run_id, decision_card_json FROM hitl_decisions WHERE status='PENDING' "
                     "AND reversibility='REVERSIBLE' AND digest_batch_id <= %s", (bid,)).fetchall()
    if rows:
        payload = {"batch": bid, "items": [{"decision_id": r["decision_id"], "run_id": str(r["run_id"]),
                                            "card": r["decision_card_json"]} for r in rows]}
        try:
            infra.valkey().publish("agdd:hitl:digest", json.dumps(payload, ensure_ascii=False, default=str))
            infra.valkey().set(f"agdd:digest:{bid}", json.dumps(payload, default=str), ex=7 * 86400)
        except Exception as e:
            print(f"[scheduler] digest publish failed {e!r}", flush=True)
    return len(rows)


def main():
    infra.wait_for_schema()
    last_digest = None
    last_daily = None
    last_expire = None
    print("[scheduler] started", flush=True)
    while True:
        now = datetime.now()
        try:
            with infra.pg() as c:
                n = c.execute(orchestrator.REAP_EXPIRED_SQL).rowcount
                if n:
                    print(f"[scheduler] re-queued {n} expired leases", flush=True)
                # runs whose worker heartbeat is gone are recovered by lease expiry above
                for t in hitl.DIGEST_TIMES:
                    slot = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                    if slot <= now < slot + timedelta(minutes=5) and last_digest != slot:
                        print(f"[scheduler] digest {slot:%H:%M}: {build_digest(c, slot)} items", flush=True)
                        last_digest = slot
                if last_daily != now.date():
                    ensure_partitions(c, now.date())
                    last_daily = now.date()
                c.commit()
            if now.hour == 3 and last_expire != now.date():
                last_expire = now.date()
                print(f"[scheduler] expired partitions: {expire_partitions(now.date())}", flush=True)
        except Exception as e:
            print(f"[scheduler] tick failed: {e!r}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    main()
