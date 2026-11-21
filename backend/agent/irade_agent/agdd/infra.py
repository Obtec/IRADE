"""Runtime adapters for the dockerized components: PostgreSQL (primary/replica via PgBouncer),
Valkey, S3/MinIO and an OpenAI-compatible model client routed through the LLM Gateway."""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from functools import lru_cache


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"missing env {name}")
    return v


# ---------------- PostgreSQL ----------------
@lru_cache(maxsize=2)
def _pool(dsn: str):
    from psycopg_pool import ConnectionPool

    # PgBouncer transaction pooling: no server-side prepared statements.
    return ConnectionPool(dsn, min_size=1, max_size=int(os.environ.get("PG_POOL_MAX", "10")),
                          kwargs={"autocommit": False, "prepare_threshold": None}, open=True)


@contextmanager
def pg(readonly: bool = False):
    """Writes/locks/budget/HITL -> primary; dashboards & reports -> replica."""
    dsn = env("PG_REPLICA_DSN", env("PG_DSN")) if readonly else env("PG_DSN")
    with _pool(dsn).connection() as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        yield conn


def wait_for_pg(timeout: float = 90) -> None:
    import psycopg

    end = time.time() + timeout
    while True:
        try:
            psycopg.connect(env("PG_DSN"), connect_timeout=3).close()
            return
        except Exception:
            if time.time() > end:
                raise
            time.sleep(2)


def wait_for_schema(table: str = "job_queue", timeout: float = 180) -> None:
    """Workers/scheduler start only after agdd-api has applied control_plane.sql."""
    import psycopg

    wait_for_pg(timeout)
    end = time.time() + timeout
    while True:
        with psycopg.connect(env("PG_DSN"), connect_timeout=3) as c:
            if c.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]:
                return
        if time.time() > end:
            raise RuntimeError(f"schema not ready: {table}")
        time.sleep(2)


def migrate() -> None:
    import psycopg

    sql = open(os.path.join(os.path.dirname(__file__), "sql", "control_plane.sql"), encoding="utf-8").read()
    with psycopg.connect(env("PG_DIRECT_DSN", env("PG_DSN")), autocommit=True) as c:
        c.execute(sql)


# ---------------- Valkey ----------------
@lru_cache(maxsize=1)
def valkey():
    import redis

    return redis.Redis.from_url(env("VALKEY_URL", "redis://valkey:6379/0"), decode_responses=True)


def heartbeat(worker_id: str, ttl: int = 5) -> None:
    valkey().set(f"agdd:hb:{worker_id}", str(time.time()), ex=ttl * 2)


def log_stream(run_id: str, line: str) -> None:
    valkey().xadd(f"agdd:log:{run_id}", {"line": line[:4000]}, maxlen=5000, approximate=True)


def cache_get_or_set(key: str, ttl: int, producer):
    v = valkey().get(key)
    if v is not None:
        return json.loads(v)
    data = producer()
    valkey().set(key, json.dumps(data, default=str), ex=ttl)
    return data


def cache_invalidate(*keys: str) -> None:
    if keys:
        valkey().delete(*keys)


def rate_limit(key: str, limit: int, window_s: int = 60) -> bool:
    k = f"agdd:rl:{key}:{int(time.time() // window_s)}"
    n = valkey().incr(k)
    if n == 1:
        valkey().expire(k, window_s)
    return n <= limit


# ---------------- Object storage ----------------
@lru_cache(maxsize=1)
def s3():
    import boto3

    return boto3.client("s3", endpoint_url=env("S3_ENDPOINT", "http://object-storage:8333"),
                        aws_access_key_id=env("S3_ACCESS_KEY"), aws_secret_access_key=env("S3_SECRET_KEY"),
                        region_name=env("S3_REGION", "us-east-1"))


def ensure_bucket(timeout: float = 90) -> None:
    bucket = env("S3_BUCKET", "agdd")
    end = time.time() + timeout
    while True:
        try:
            try:
                s3().head_bucket(Bucket=bucket)
            except Exception as e:
                if "404" not in str(e) and "Not Found" not in str(e):
                    raise
                s3().create_bucket(Bucket=bucket)
            return
        except Exception:
            if time.time() > end:
                raise
            time.sleep(2)


def put_object(key_hint: str, body: bytes) -> str:
    """Immutable SHA-256 keyed object; returns s3:// URI."""
    bucket = env("S3_BUCKET", "agdd")
    digest = hashlib.sha256(body).hexdigest()
    prefix = key_hint.split("/", 1)[0] if "/" in key_hint else "blob"
    key = f"{prefix}/{digest[:2]}/{digest}"
    s3().put_object(Bucket=bucket, Key=key, Body=body)
    return f"s3://{bucket}/{key}"


# ---------------- Models (through LLM Gateway) ----------------
def cheap_model(task_kind: str = "classify", workspace_id=None, run_id=None, node: str | None = None):
    """Returns llm(prompt, json_schema) -> dict using structured output at thinking=none."""
    from openai import OpenAI

    client = OpenAI(base_url=env("LLM_GATEWAY_URL", "http://llm-gateway:8090/v1"),
                    api_key=env("LLM_GATEWAY_TOKEN", "internal"))
    model = env("AGDD_CHEAP_MODEL", "cheap")

    def call(prompt: str, schema: dict) -> dict:
        r = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_schema", "json_schema": {"name": "out", "schema": schema}},
            extra_headers={"x-agdd-task": task_kind, "x-agdd-workspace": str(workspace_id or ""),
                           "x-agdd-run": str(run_id or ""), "x-agdd-node": node or ""},
        )
        return parse_json(r.choices[0].message.content or "")
    return call


def parse_json(text: str) -> dict:
    """Tolerates code fences / leading prose around the JSON object."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                pass
    print(f"[agdd] unparseable model JSON: {text[:200]!r}", flush=True)
    return {}


def embedder():
    from openai import OpenAI

    model = os.environ.get("AGDD_EMBED_MODEL")
    if not model:
        from .research import hash_embed
        return hash_embed
    client = OpenAI(base_url=env("LLM_GATEWAY_URL", "http://llm-gateway:8090/v1"),
                    api_key=env("LLM_GATEWAY_TOKEN", "internal"))
    return lambda text: client.embeddings.create(model=model, input=text[:8000]).data[0].embedding
