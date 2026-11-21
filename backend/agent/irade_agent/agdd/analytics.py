"""Long-term token/quality OLAP (DOC.md REQ-006, PHASE_3).

node_executions partitions are exported from a Read Replica to Parquet in object
storage, then aggregated with DuckDB so the Primary RDB sees zero analytic I/O.
"""
from __future__ import annotations

from pathlib import Path

EXPORT_SQL = """
SELECT exec_id, run_id::text AS run_id, node_name, model_name, prompt_tokens, cached_tokens, output_tokens,
       thinking_tokens, cost_usd::float8 AS cost_usd, duration_ms, created_at
  FROM node_executions
 WHERE created_at >= %(start)s AND created_at < %(end)s
"""

AGG_SQL = """
SELECT model_name,
       date_trunc('day', created_at)                         AS day,
       count(*)                                              AS calls,
       sum(prompt_tokens)                                    AS prompt_tokens,
       sum(cached_tokens)::double / nullif(sum(prompt_tokens), 0) AS cache_hit_ratio,
       sum(thinking_tokens)::double / nullif(sum(output_tokens + thinking_tokens), 0) AS thinking_share,
       sum(cost_usd)                                         AS cost_usd,
       avg(duration_ms)                                      AS avg_ms
  FROM read_parquet(?)
 WHERE created_at >= now() - to_days(CAST(? AS INTEGER))
 GROUP BY ALL
 ORDER BY day, model_name
"""

def export_month(pg_conn, start: str, end: str, out_dir: str | Path) -> Path:
    """Stream rows from the replica into a Parquet file via DuckDB (no pandas)."""
    import duckdb

    out = Path(out_dir) / f"node_executions_{start[:7]}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    with pg_conn.cursor() as cur:
        cur.execute(EXPORT_SQL, {"start": start, "end": end})
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    con = duckdb.connect()
    con.execute("CREATE TABLE t (" + ", ".join(
        f"{c} {'TIMESTAMPTZ' if c == 'created_at' else 'DOUBLE' if c == 'cost_usd' else 'VARCHAR' if c in ('run_id', 'node_name', 'model_name') else 'BIGINT'}"
        for c in cols) + ")")
    if rows:
        con.executemany(f"INSERT INTO t VALUES ({', '.join('?' * len(cols))})", rows)
    con.execute(f"COPY t TO '{out.as_posix()}' (FORMAT PARQUET)")
    return out


def aggregate(parquet_glob: str) -> list[dict]:
    import duckdb

    con = duckdb.connect()
    cur = con.execute(AGG_SQL, [parquet_glob])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def s3_connection():
    """DuckDB with httpfs configured for MinIO/S3 (reads archive/node_executions/*.parquet)."""
    import duckdb
    import os
    from urllib.parse import urlparse

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    ep = urlparse(os.environ.get("S3_ENDPOINT", "http://object-storage:8333"))
    con.execute("SET s3_endpoint=?", [ep.netloc])
    con.execute("SET s3_use_ssl=?", [ep.scheme == "https"])
    con.execute("SET s3_url_style='path'")
    con.execute("SET s3_access_key_id=?", [os.environ["S3_ACCESS_KEY"]])
    con.execute("SET s3_secret_access_key=?", [os.environ["S3_SECRET_KEY"]])
    con.execute("SET s3_region=?", [os.environ.get("S3_REGION", "us-east-1")])
    return con


def main():
    """PHASE_3 OLAP job: aggregate archived Parquet (never touches the Primary RDB)."""
    import json
    import os

    import tempfile
    from datetime import date

    import psycopg

    from . import infra

    bucket = os.environ.get("S3_BUCKET", "agdd")
    days = int(os.environ.get("ANALYTICS_DAYS", "90"))
    # refresh recent months from the Read Replica so the Primary sees no analytic I/O
    today = date.today()
    with psycopg.connect(os.environ.get("PG_REPLICA_DSN") or os.environ["PG_DSN"]) as conn, \
            tempfile.TemporaryDirectory() as d:
        for k in range(0, (days // 30) + 2):
            y, m = divmod(today.month - 1 - k, 12)
            start = date(today.year + y, m + 1, 1)
            y2, m2 = divmod(start.month, 12)
            end = date(start.year + y2, m2 + 1, 1)
            out = export_month(conn, start.isoformat(), end.isoformat(), d)
            infra.s3().put_object(Bucket=bucket, Key=f"archive/node_executions/recent_{start:%Y_%m}.parquet",
                                  Body=out.read_bytes())
    con = s3_connection()
    cur = con.execute(AGG_SQL, [f"s3://{bucket}/archive/node_executions/*.parquet", days])
    cols = [d[0] for d in cur.description]
    for r in cur.fetchall():
        print(json.dumps(dict(zip(cols, r)), default=str))


if __name__ == "__main__":
    main()
