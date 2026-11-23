"""On-Demand Research Engine (DOC.md §5.6, REQ-005 / ARC-005).

Order: (1) reuse past sessions by vector similarity -> (2) sanitize the problem
signature -> (3) parallel search (arXiv, Semantic Scholar, configured doc sites)
-> raw text to object storage -> FTS5 excerpt -> <=1,000-token Insight Brief
with source links on every claim.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .gateway import estimate_tokens

CheapModel = Callable[[str, dict], dict]
Embed = Callable[[str], list[float]]
BRIEF_MAX_TOKENS = 1000
USER_AGENT = "agdd-research/1.0"

# ---------------- sanitization ----------------
_SECRET = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|authorization)\s*[:=]\s*\S+"),
    re.compile(r"\b(sk|pk|ghp|gho|xox[bp])[-_][A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]+?-----END [A-Z ]+-----"),
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
]
_PII = [re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), re.compile(r"\b\d{2,3}-\d{3,4}-\d{4}\b"),
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), re.compile(r"https?://\S+")]
_PATHS = re.compile(r"(?:[A-Za-z]:)?(?:/[\w.\-]+){2,}")
_ERRCLASS = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Fault|Panic))\b")
_LIBVER = re.compile(r"\b([a-z][a-z0-9_.\-]{1,40})[ =@v]+(\d+\.\d+(?:\.\d+)?)\b", re.I)


def sanitize(text: str, internal_names: list[str] = ()) -> str:
    out = text
    for rx in _SECRET + _PII:
        out = rx.sub("<redacted>", out)
    out = _PATHS.sub("<path>", out)
    for name in sorted(set(internal_names), key=len, reverse=True):
        if name:
            out = re.sub(re.escape(name), "<internal>", out, flags=re.I)
    return out


def build_query(problem: str, internal_names: list[str] = ()) -> str:
    """Keep only error class, library@version and generic algorithm keywords."""
    clean = sanitize(problem, internal_names)
    errs = list(dict.fromkeys(_ERRCLASS.findall(clean)))
    libs = list(dict.fromkeys(f"{l} {v}" for l, v in _LIBVER.findall(clean) if "<" not in l))
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", clean)
             if w.lower() not in {"redacted", "internal", "path", "error", "exception", "line", "file", "traceback",
                                  "with", "from", "when", "this", "that", "into", "have", "after", "before"}]
    taken = {t.lower() for t in " ".join(errs[:2] + libs[:2]).split()}
    generic = [w for w in dict.fromkeys(w.lower() for w in words) if w not in taken][:6]
    q = " ".join(errs[:2] + libs[:2] + generic)
    assert not any(n and n.lower() in q.lower() for n in internal_names)
    return q.strip()


def leaked(query: str, internal_names: list[str]) -> bool:
    return any(rx.search(query) for rx in _SECRET + _PII) or any(n and n.lower() in query.lower() for n in internal_names)


# ---------------- providers ----------------
@dataclass
class Source:
    title: str
    url: str
    text: str
    provider: str


class _Throttle:
    """Process-wide request pacing for an external API (one request in flight, ``interval`` s apart)."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._last = 0.0

    def __enter__(self):
        self._lock.acquire()
        wait = self._last + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        return self

    def __exit__(self, *exc):
        self._last = time.monotonic()
        self._lock.release()


# arXiv API Terms of Use: at most one request every 3 seconds on a single connection, and products must show
# "Thank you to arXiv for use of its open access interoperability." (https://info.arxiv.org/help/api/tou.html)
ARXIV_THROTTLE = _Throttle(3.0)
# Semantic Scholar API License Agreement: respect the per-key rate limit (1 request/second) and attribute
# Semantic Scholar as the data source (https://www.semanticscholar.org/product/api/license).
S2_THROTTLE = _Throttle(1.0)
ATTRIBUTIONS = {
    "arxiv": "Thank you to arXiv for use of its open access interoperability.",
    "semantic_scholar": "Paper data from the Semantic Scholar API (https://www.semanticscholar.org).",
}


class Provider(Protocol):
    name: str

    def search(self, query: str, limit: int) -> list[Source]: ...


def _get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https hosts)
        return r.read()


class ArxivProvider:
    name = "arxiv"

    def search(self, query, limit=5):
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
            {"search_query": " AND ".join(f"all:{w}" for w in query.split()[:4]), "max_results": limit,
             "sortBy": "relevance"})
        ns = {"a": "http://www.w3.org/2005/Atom"}
        with ARXIV_THROTTLE:
            data = _get(url)
        root = ET.fromstring(data)
        out = []
        for e in root.findall("a:entry", ns):
            out.append(Source(" ".join((e.findtext("a:title", "", ns)).split()), e.findtext("a:id", "", ns),
                              " ".join(e.findtext("a:summary", "", ns).split()), self.name))
        return out


class SemanticScholarProvider:
    name = "semantic_scholar"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key

    def search(self, query, limit=5):
        url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(
            {"query": query, "limit": limit, "fields": "title,abstract,url"})
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **({"x-api-key": self.api_key}
                                                                                if self.api_key else {})})
        with S2_THROTTLE, urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
            data = json.loads(r.read())
        return [Source(p.get("title") or "", p.get("url") or "", p.get("abstract") or "", self.name)
                for p in data.get("data", []) if p.get("abstract")]


class DocSiteProvider:
    """Official docs / RFC / issue trackers via an allowlisted search endpoint template returning JSON
    [{title,url,text}] (e.g. a self-hosted SearXNG/Elasticsearch proxy)."""
    name = "docs"

    def __init__(self, endpoint_template: str):
        self.tpl = endpoint_template

    def search(self, query, limit=5):
        data = json.loads(_get(self.tpl.format(q=urllib.parse.quote(query), n=limit)))
        items = data.get("results", data) if isinstance(data, dict) else data
        return [Source(i.get("title", ""), i.get("url", ""), i.get("content") or i.get("text", ""), self.name)
                for i in items[:limit] if i.get("url")]


# ---------------- reuse store ----------------
class ResearchStore(Protocol):
    def nearest(self, embedding: list[float], k: int, min_sim: float) -> list[dict]: ...
    def save(self, row: dict) -> int: ...


def cosine(a, b):
    num = sum(x * y for x, y in zip(a, b))
    den = (sum(x * x for x in a) ** .5) * (sum(y * y for y in b) ** .5)
    return num / den if den else 0.0


class MemoryResearchStore:
    def __init__(self):
        self.rows: list[dict] = []

    def nearest(self, embedding, k, min_sim):
        scored = sorted(((cosine(embedding, r["query_embedding"]), r) for r in self.rows
                         if r.get("insight_brief_md")), key=lambda x: -x[0])
        return [dict(r, similarity=s) for s, r in scored[:k] if s >= min_sim]

    def save(self, row):
        self.rows.append(dict(row, research_id=len(self.rows) + 1))
        return len(self.rows)


# ---------------- brief ----------------
BRIEF_SCHEMA = {"type": "object", "properties": {
    "hypotheses": {"type": "array", "items": {"type": "string"}},
    "principles": {"type": "array", "items": {"type": "object", "properties": {
        "claim": {"type": "string"}, "source": {"type": "integer"}}}},
    "tradeoffs": {"type": "array", "items": {"type": "string"}}},
    "required": ["hypotheses", "principles", "tradeoffs"]}
BRIEF_PROMPT = """교착 문제에 대한 Insight Brief를 작성하세요. 아래 발췌만 근거로 사용하고, principles 각 항목에
근거 발췌 번호(source)를 반드시 지정하세요. hypotheses(원인 가설), principles(핵심 원리), tradeoffs(적용 트레이드오프).
"""


def excerpt(sources: list[Source], query: str, k: int = 8, chunk: int = 700) -> list[tuple[int, str]]:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE c USING fts5(src UNINDEXED, body, tokenize='unicode61')")
    for i, s in enumerate(sources):
        text = f"{s.title}. {s.text}"
        for j in range(0, len(text), chunk):
            db.execute("INSERT INTO c VALUES (?,?)", (i, text[j:j + chunk]))
    terms = " OR ".join(f'"{w}"' for w in re.findall(r"\w{3,}", query)[:12])
    try:
        rows = db.execute("SELECT src, body FROM c WHERE c MATCH ? ORDER BY bm25(c) LIMIT ?", (terms, k)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        rows = db.execute("SELECT src, body FROM c LIMIT ?", (k,)).fetchall()
    return [(int(s), b) for s, b in rows]


def render_brief(problem_sig: str, brief: dict, sources: list[Source]) -> str:
    lines = [f"# Insight Brief", f"문제 시그니처: `{problem_sig}`", "", "## 원인 가설"]
    lines += [f"- {h}" for h in brief.get("hypotheses", [])[:4]]
    lines += ["", "## 핵심 원리"]
    for p in brief.get("principles", [])[:6]:
        i = p.get("source")
        if isinstance(i, int) and 0 <= i < len(sources):  # claims without a valid source are dropped
            lines.append(f"- {p.get('claim', '')} [{i + 1}]")
    lines += ["", "## 적용 트레이드오프"] + [f"- {t}" for t in brief.get("tradeoffs", [])[:4]]
    lines += ["", "## 출처"] + [f"{i + 1}. [{s.title}]({s.url})" for i, s in enumerate(sources)]
    attr = [f"_{ATTRIBUTIONS[p]}_" for p in dict.fromkeys(s.provider for s in sources) if p in ATTRIBUTIONS]
    lines += ([""] + attr) if attr else []  # provider terms require attribution wherever results are shown
    tail = len(sources) + 3 + (len(attr) + 1 if attr else 0)
    md = "\n".join(lines)
    while estimate_tokens(md) > BRIEF_MAX_TOKENS and len(lines) > 8 + tail - len(sources) - 3:
        lines.pop(len(lines) - tail)  # trim body before the source list and attributions
        md = "\n".join(lines)
    return md


@dataclass
class ResearchEngine:
    providers: list
    llm: CheapModel
    embed: Embed
    store: ResearchStore = field(default_factory=MemoryResearchStore)
    put_object: Callable[[str, bytes], str] | None = None   # object storage writer -> uri
    internal_names: list[str] = field(default_factory=list)
    reuse_threshold: float = 0.88

    def run(self, problem: str, trigger_source: str = "HUMAN", run_id: str | None = None) -> dict:
        query = build_query(problem, self.internal_names)
        if leaked(query, self.internal_names):
            raise ValueError("sanitization failed; external query blocked")
        emb = self.embed(query)
        hit = self.store.nearest(emb, 1, self.reuse_threshold)
        if hit:
            return {"reused": True, "research_id": hit[0]["research_id"], "insight_brief_md": hit[0]["insight_brief_md"],
                    "query": query}
        with ThreadPoolExecutor(max_workers=max(1, len(self.providers))) as ex:
            futures = [ex.submit(p.search, query, 5) for p in self.providers]
            sources: list[Source] = []
            errors = []
            for f in futures:
                try:
                    sources += f.result()
                except Exception as e:  # one provider failing must not block the brief
                    errors.append(repr(e)[:200])
        seen, uniq = set(), []
        for s in sources:
            if s.url and s.url not in seen:
                seen.add(s.url)
                uniq.append(s)
        uris = []
        for s in uniq:
            if self.put_object:
                body = json.dumps(s.__dict__, ensure_ascii=False).encode()
                uris.append(self.put_object(f"research/{hashlib.sha256(body).hexdigest()}.json", body))
        parts = excerpt(uniq, query)
        prompt = BRIEF_PROMPT + json.dumps({"problem": query, "excerpts": [
            {"source": i, "title": uniq[i].title, "text": t} for i, t in parts]}, ensure_ascii=False)
        brief_md = render_brief(query, self.llm(prompt, BRIEF_SCHEMA), uniq) if uniq else \
            f"# Insight Brief\n문제 시그니처: `{query}`\n\n검색 결과 없음. 오류: {errors}"
        rid = self.store.save({"run_id": run_id, "trigger_source": trigger_source, "problem_signature": query,
                               "query_embedding": emb,
                               "sources_json": [{"title": s.title, "url": s.url, "provider": s.provider} for s in uniq],
                               "object_uris": uris, "insight_brief_md": brief_md})
        return {"reused": False, "research_id": rid, "insight_brief_md": brief_md, "query": query, "errors": errors}


def hash_embed(text: str, dim: int = 1536) -> list[float]:
    """Deterministic fallback embedding (hashed bag of words) when no embedding model is configured."""
    v = [0.0] * dim
    for w in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % dim] += 1.0 if (h >> 8) & 1 else -1.0
    return v
