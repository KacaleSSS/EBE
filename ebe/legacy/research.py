"""Resumable bounded discovery and acquisition; evidence judgment remains explicit."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from threading import BoundedSemaphore
from pathlib import Path
from typing import Any

import engine
from pipeline import project_lock
from storage import batch_durable_sync


PROVIDERS = {"brave", "openalex", "codex"}


class ProviderError(ValueError):
    def __init__(self, code: str, retry_after: int = 0):
        super().__init__(code)
        self.retry_after = retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        # Provider credentials must never follow a redirect to a different host.
        raise ProviderError("provider_redirect_refused")


def provider_request(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
            raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ProviderError("provider_response_too_large")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ProviderError("provider_response_not_object")
        return data
    except urllib.error.HTTPError as exc:
        retry = exc.headers.get("Retry-After", "0") if exc.headers else "0"
        delay = min(3600, int(retry)) if retry.isdigit() else 60
        raise ProviderError(f"provider_http_{exc.code}", delay if exc.code == 429 or exc.code >= 500 else 0) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise ProviderError("provider_network_or_decode_error", 30) from None


def search_provider(provider: str, query: str, page: int) -> dict[str, Any]:
    if provider == "brave":
        key = os.environ.get("BRAVE_SEARCH_API_KEY")
        if not key:
            raise ProviderError("missing_BRAVE_SEARCH_API_KEY")
        data = provider_request("https://api.search.brave.com/res/v1/web/search?" +
                                urllib.parse.urlencode({"q": query, "count": 20, "offset": page}),
                                {"X-Subscription-Token": key})
        candidates = [{"url": row["url"], "title": row.get("title", row["url"]),
                       "snippet": row.get("description", ""), "published_at": row.get("page_age"),
                       "source_type": "web"} for row in (data.get("web") or {}).get("results", [])
                      if isinstance(row, dict) and isinstance(row.get("url"), str)]
        return {"candidates": candidates, "has_more": bool(data.get("query", {}).get("more_results_available")) and page < 9}
    if provider == "openalex":
        key = os.environ.get("OPENALEX_API_KEY")
        data = provider_request("https://api.openalex.org/works?" + urllib.parse.urlencode(
            {"search": query, "per_page": 20, "page": page + 1}),
            {"Authorization": f"Bearer {key}"} if key else {})
        candidates = []
        for row in data.get("results", []):
            location = row.get("best_oa_location") or row.get("primary_location") or {}
            url = location.get("pdf_url") or location.get("landing_page_url") or row.get("doi")
            if not url:
                continue
            candidates.append({"url": url, "title": row.get("title") or url,
                               "published_at": row.get("publication_date"), "source_type": "academic",
                               "publisher": (location.get("source") or {}).get("display_name"),
                               "doi": row.get("doi"), "openalex_id": row.get("id")})
        return {"candidates": candidates, "has_more": (page + 1) * 20 < (data.get("meta", {}).get("count") or 0)}
    raise ProviderError("codex_search_requires_submitted_results")


def connect(project: Path):
    conn = engine.connect(project)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS research_queries (
          query_id TEXT PRIMARY KEY, query TEXT NOT NULL, dimension TEXT NOT NULL,
          provider TEXT NOT NULL, page INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          not_before REAL NOT NULL DEFAULT 0, last_error TEXT);
        CREATE TABLE IF NOT EXISTS acquisitions (
          source_id TEXT PRIMARY KEY, query_id TEXT NOT NULL, identity TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          not_before REAL NOT NULL DEFAULT 0, last_error TEXT, result_json TEXT);
        CREATE TABLE IF NOT EXISTS acquisition_batches (
          batch_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, source_ids TEXT NOT NULL);
    """)
    conn.commit()
    return conn


def clean_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(engine.canonical_url(value))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("candidate URL must be public HTTP(S) without credentials")
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/",
                                   urllib.parse.urlencode(query), ""))


def add_queries(project: Path, queries: list[dict[str, Any]]) -> list[str]:
    normalized = []
    for item in queries:
        query = item.get("query")
        provider = item.get("provider", "codex")
        dimension = item.get("dimension")
        if not isinstance(query, str) or not query.strip() or len(query) > 600 or len(query.split()) > 75:
            raise ValueError("query must contain 1-600 characters and at most 75 words")
        if provider not in PROVIDERS or dimension not in engine.RESEARCH_DIMENSIONS:
            raise ValueError("invalid provider or research dimension")
        identity = engine.sha256_text(json.dumps([query.strip(), provider, dimension]))[:24]
        normalized.append((identity, query.strip(), dimension, provider))
    with closing(connect(project)) as conn:
        conn.executemany("INSERT OR IGNORE INTO research_queries(query_id,query,dimension,provider) VALUES (?,?,?,?)", normalized)
        conn.commit()
    engine.sync_durable_state(project)
    return [row[0] for row in normalized]


def plan_research(project_dir: str, providers: list[str] | None = None,
                  queries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    project = engine.require_project(project_dir)
    with project_lock(project):
        if queries is None:
            providers = providers or ["codex"]
            if not providers or any(p not in PROVIDERS for p in providers):
                raise ValueError("unsupported research provider")
            topic = engine.read_project_metadata(project)["topic"]
            readiness = engine.research_readiness(project)
            dimensions = [d for d in engine.RESEARCH_DIMENSIONS if readiness["coverage"].get(d, 0) < 3]
            dimensions = dimensions or list(engine.RESEARCH_DIMENSIONS)
            queries = [{"query": f"{topic} {d.replace('_', ' ')} {angle}", "provider": p, "dimension": d}
                       for d in dimensions for angle in ("evidence report filetype:pdf", "failure alternatives case study")
                       for p in providers]
        if not 1 <= len(queries) <= 100:
            raise ValueError("submit 1-100 queries per plan")
        ids = add_queries(project, queries)
        return {"query_ids": ids, "count": len(ids), "minimum_sources": engine.MIN_QUALIFIED_SOURCES}


def register_candidates(project: Path, query: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, int]:
    added = duplicates = rejected = 0
    for item in candidates:
        if (not isinstance(item, dict) or not isinstance(item.get("url"), str) or
                any(item.get(field) is not None and not isinstance(item[field], str)
                    for field in ("title", "doi", "publisher", "published_at", "source_type"))):
            rejected += 1
            continue
        try:
            url = clean_url(item["url"])
        except ValueError:
            rejected += 1
            continue
        doi = str(item.get("doi") or "").lower().removeprefix("https://doi.org/").removeprefix("doi:").strip()
        identity = "doi:" + doi if doi else url
        with closing(connect(project)) as conn:
            existing = conn.execute("SELECT source_id FROM acquisitions WHERE identity=?", (identity,)).fetchone()
        if existing:
            duplicates += 1
            continue
        registered = engine.register_sources(project, [{"url": url, "title": str(item.get("title") or url)[:1000],
            "publisher": item.get("publisher"), "published_at": item.get("published_at"),
            "source_type": item.get("source_type", "web"), "status": "selected",
            "search_query": query["query"], "selected_reason": "discovery candidate; quality and relevance unverified",
            "metadata": {"discovery_provider": query["provider"], "dimension": query["dimension"],
                         "doi": doi, "snippet_is_evidence": False}}])["sources"][0]
        source_id = registered["source_id"]
        with closing(connect(project)) as conn:
            exists = conn.execute("SELECT source_id FROM acquisitions WHERE source_id=?", (source_id,)).fetchone()
            if exists:
                duplicates += 1
                continue
            conn.execute("INSERT INTO acquisitions(source_id,query_id,identity) VALUES (?,?,?)",
                         (source_id, query["query_id"], identity))
            conn.commit()
        added += 1
    engine.sync_durable_state(project)
    return {"added": added, "duplicates": duplicates, "rejected": rejected}


def submit_search_results(project_dir: str, query_id: str, page: int,
                           candidates: list[dict[str, Any]], has_more: bool = False) -> dict[str, Any]:
    if not 0 <= page <= 9 or len(candidates) > 100:
        raise ValueError("invalid page or candidate count")
    project = engine.require_project(project_dir)
    with project_lock(project):
        with closing(connect(project)) as conn:
            row = conn.execute("SELECT * FROM research_queries WHERE query_id=?", (query_id,)).fetchone()
        if not row or row["provider"] != "codex":
            raise ValueError("query_id must reference a Codex search request")
        if page != row["page"] or row["status"] == "done":
            return {"status": "stale_page", "expected_page": row["page"]}
        counts = register_candidates(project, dict(row), candidates)
        with closing(connect(project)) as conn:
            conn.execute("UPDATE research_queries SET page=page+1,status=?,attempts=0,last_error=NULL WHERE query_id=?",
                         ("pending" if has_more and page < 9 else "done", query_id))
            conn.commit()
        engine.sync_durable_state(project)
        return {"status": "recorded", **counts}


def research_status(project_dir: str) -> dict[str, Any]:
    project = engine.require_project(project_dir)
    with closing(connect(project)) as conn:
        queries = [dict(r) for r in conn.execute("SELECT * FROM research_queries ORDER BY rowid")]
        counts = {r[0]: r[1] for r in conn.execute("SELECT status,COUNT(*) FROM acquisitions GROUP BY status")}
        batches = [{"batch_id": r[0], "source_ids": json.loads(r[1])} for r in
                   conn.execute("SELECT batch_id,source_ids FROM acquisition_batches ORDER BY batch_id DESC LIMIT 2")]
    return {"queries": queries, "acquisitions": counts, "recent_batches": batches,
            "readiness": engine.research_readiness(project),
            "configured": {"brave": bool(os.environ.get("BRAVE_SEARCH_API_KEY")),
                           "openalex_key": bool(os.environ.get("OPENALEX_API_KEY")), "codex": True}}


def assess_source(project: Path, source_id: str, min_chars: int = 800) -> dict[str, Any]:
    with closing(engine.connect(project)) as conn:
        row = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if not row or row["status"] != "ingested" or not row["text_path"]:
            return {"status": "failed", "reason": "not_readable"}
        text = (project / row["text_path"]).read_text(encoding="utf-8")
        normalized_hash = engine.sha256_text(re.sub(r"\s+", " ", text).strip().casefold())
        reason = None
        if len(text.strip()) < min_chars:
            reason = "insufficient_body"
        if re.search(r"(verify you are human|access denied|captcha|enable javascript)", text[:1500], re.I) and len(text) < 4000:
            reason = "access_barrier"
        for other in conn.execute("SELECT source_id,metadata_json FROM sources WHERE source_id<>? AND status IN ('ingested','awaiting-review')", (source_id,)):
            if json.loads(other["metadata_json"]).get("normalized_text_hash") == normalized_hash:
                reason = "duplicate_text"
                break
        metadata = json.loads(row["metadata_json"])
        metadata.update(normalized_text_hash=normalized_hash, quality={"body_chars": len(text),
                        "publication_date_known": bool(row["published_at"]), "publisher_known": bool(row["publisher"]),
                        "authority": "unverified", "relevance": "requires_evidence_review", "exclusion": reason})
        conn.execute("UPDATE sources SET status=?, excluded_reason=?,metadata_json=? WHERE source_id=?",
                     ("excluded" if reason else "awaiting-review", reason, json.dumps(metadata), source_id))
        conn.commit()
    engine.sync_durable_state(project)
    return {"status": "excluded" if reason else "awaiting-review", "reason": reason,
            "source_id": source_id, "quality": metadata["quality"]}


def run_research_batch(project_dir: str, batch_size: int = 20, max_queries: int = 2,
                        max_attempts: int = 3, workers: int = 4) -> dict[str, Any]:
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    project = engine.require_project(project_dir)
    with project_lock(project):
        with batch_durable_sync(project):
            result = _run_research_batch(project_dir, batch_size, max_queries, max_attempts, workers)
        verification = engine.verify_knowledge_base(project)
        if not verification["pass"]:
            raise ValueError("knowledge base failed verification after acquisition")
        return result


def _download_candidates(project: Path, pending: list[dict[str, Any]], workers: int):
    # Materialize plain data before starting workers. No connection crosses threads.
    with closing(engine.connect(project)) as conn:
        sources = {r["source_id"]: dict(r) for r in conn.execute("SELECT * FROM sources")}
    domains: dict[str, BoundedSemaphore] = {}
    for candidate in pending:
        source = sources[candidate["source_id"]]
        host = urllib.parse.urlsplit(source["url"]).hostname
        domains.setdefault(host, BoundedSemaphore(2))

    def download(candidate):
        source = sources[candidate["source_id"]]
        if workers == 1 or source["status"] in {"ingested", "excluded", "awaiting-review"}:
            return None
        try:
            host = urllib.parse.urlsplit(source["url"]).hostname
            with domains[host]:
                return engine.download_url(source["url"])
        except Exception as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=workers) as pool:
        remaining = iter(pending)
        queued = deque()
        for _ in range(min(workers, len(pending))):
            candidate = next(remaining)
            queued.append((candidate, pool.submit(download, candidate)))
        while queued:
            candidate, future = queued.popleft()
            yield candidate, future.result()
            candidate = next(remaining, None)
            if candidate is not None:
                queued.append((candidate, pool.submit(download, candidate)))


def _run_research_batch(project_dir: str, batch_size: int, max_queries: int,
                        max_attempts: int, workers: int) -> dict[str, Any]:
    if not 1 <= batch_size <= 20 or not 0 <= max_queries <= 5 or not 1 <= max_attempts <= 5:
        raise ValueError("invalid acquisition limits")
    project = engine.require_project(project_dir)
    searches = []
    with closing(connect(project)) as conn:
        queries = [dict(r) for r in conn.execute(
            "SELECT * FROM research_queries WHERE status!='done' AND attempts<? AND not_before<=? ORDER BY rowid LIMIT ?",
            (max_attempts, time.time(), max_queries))]
    for query in queries:
        if query["provider"] == "codex":
            searches.append({"status": "needs_search", "query_id": query["query_id"],
                             "query": query["query"], "page": query["page"], "dimension": query["dimension"]})
            continue
        try:
            response = search_provider(query["provider"], query["query"], query["page"])
            counts = register_candidates(project, query, response["candidates"])
            with closing(connect(project)) as conn:
                conn.execute("UPDATE research_queries SET page=page+1,status=?,attempts=0,last_error=NULL WHERE query_id=?",
                             ("pending" if response["has_more"] and query["page"] < 9 else "done", query["query_id"]))
                conn.commit()
            searches.append({"query_id": query["query_id"], **counts})
        except (ProviderError, ValueError, KeyError, TypeError) as exc:
            message = "invalid_provider_payload"
            if isinstance(exc, ProviderError):
                code = str(exc)
                message = code if (re.fullmatch(r"provider_http_[0-9]{3}", code) or code in {
                    "provider_redirect_refused", "provider_response_too_large", "provider_response_not_object",
                    "provider_network_or_decode_error", "missing_BRAVE_SEARCH_API_KEY",
                    "codex_search_requires_submitted_results"}) else "ProviderError"
            delay = max(2 ** (query["attempts"] + 1), getattr(exc, "retry_after", 0))
            with closing(connect(project)) as conn:
                conn.execute("UPDATE research_queries SET attempts=attempts+1,status='failed',last_error=?,not_before=? WHERE query_id=?",
                             (message, time.time() + delay, query["query_id"]))
                conn.commit()
            searches.append({"query_id": query["query_id"], "error": message, "retry_after": delay})
        engine.sync_durable_state(project)
    with closing(connect(project)) as conn:
        pending = [dict(r) for r in conn.execute(
            "SELECT * FROM acquisitions WHERE status IN ('pending','failed') AND attempts<? AND not_before<=? ORDER BY rowid LIMIT ?",
            (max_attempts, time.time(), batch_size))]
    with closing(_download_candidates(project, pending, workers)) as downloads:
        return _commit_candidates(project, project_dir, downloads, searches)


def _commit_candidates(project, project_dir, downloads, searches):
    acquired = []
    results = []
    for candidate, downloaded in downloads:
        source_id = candidate["source_id"]
        try:
            with closing(engine.connect(project)) as conn:
                source = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
                if source["status"] not in {"ingested", "excluded", "awaiting-review"}:
                    conn.execute("UPDATE sources SET status='selected' WHERE source_id=?", (source_id,))
                    conn.commit()
            if source["status"] in {"excluded", "awaiting-review"}:
                result = {"source_id": source_id, "status": source["status"]}
            else:
                if source["status"] != "ingested":
                    if isinstance(downloaded, str):
                        result = {"source_id": source_id, "status": "failed", "error": downloaded}
                    else:
                        result = (engine.ingest_url(project, source_id) if downloaded is None
                                  else engine.commit_download(project, source_id, downloaded))
                    if result["status"] == "fetch-failed":
                        result["status"] = "failed"
                else:
                    result = {"status": "ingested"}
                if result["status"] != "failed":
                    result = assess_source(project, source_id)
        except Exception as exc:
            result = {"source_id": source_id, "status": "failed", "error": type(exc).__name__}
        status = result["status"]
        with closing(connect(project)) as conn:
            conn.execute("UPDATE acquisitions SET status=?,attempts=attempts+1,not_before=?,result_json=?,last_error=? WHERE source_id=?",
                         (status, time.time() + 2 ** (candidate["attempts"] + 1) if status == "failed" else 0,
                          json.dumps(result), result.get("error"), source_id))
            if status in {"ingested", "awaiting-review"}:
                batch = conn.execute("SELECT batch_id,source_ids FROM acquisition_batches ORDER BY batch_id DESC LIMIT 1").fetchone()
                ids = json.loads(batch[1]) if batch else []
                if batch and len(ids) < 20:
                    if source_id not in ids:
                        ids.append(source_id)
                        conn.execute("UPDATE acquisition_batches SET source_ids=? WHERE batch_id=?", (json.dumps(ids), batch[0]))
                else:
                    conn.execute("INSERT INTO acquisition_batches(created_at,source_ids) VALUES (?,?)", (engine.now_iso(), json.dumps([source_id])))
            conn.commit()
        engine.sync_durable_state(project)
        results.append(result)
        if status in {"ingested", "awaiting-review"}:
            acquired.append(source_id)
    return {"searches": searches, "results": results, "acquired_source_ids": acquired,
            "status": "needs_evidence_review" if acquired else "needs_search" if any(s.get("status") == "needs_search" for s in searches) else "no_progress",
            "research": research_status(project_dir),
            "next_action": "Read acquired bodies, update evidence-map and source-note artifacts, expand gap queries, then submit a truthful saturation report to the pipeline."}


def read_source_body(project_dir: str, source_id: str, offset: int = 0, max_chars: int = 12000) -> dict[str, Any]:
    if offset < 0 or not 100 <= max_chars <= 30000:
        raise ValueError("invalid body window")
    project = engine.require_project(project_dir)
    with closing(engine.connect(project)) as conn:
        row = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
    if not row or not row["text_path"]:
        raise ValueError("source has no extracted body")
    body = (project / row["text_path"]).read_text(encoding="utf-8")
    return {"source_id": source_id, "title": row["title"], "url": row["url"], "status": row["status"],
            "body": body[offset:offset + max_chars], "total_chars": len(body),
            "next_offset": offset + max_chars if offset + max_chars < len(body) else None,
            "metadata": json.loads(row["metadata_json"])}


def retry_research(project_dir: str) -> dict[str, Any]:
    """Explicit retry after credentials, network or query issues have been addressed."""
    project = engine.require_project(project_dir)
    with project_lock(project):
        with closing(connect(project)) as conn:
            queries = conn.execute("UPDATE research_queries SET attempts=0,not_before=0,status='pending',last_error=NULL WHERE status='failed'").rowcount
            sources = conn.execute("UPDATE acquisitions SET attempts=0,not_before=0,status='pending',last_error=NULL WHERE status='failed'").rowcount
            conn.commit()
        engine.sync_durable_state(project)
        return {"queries_reset": queries, "sources_reset": sources}


def review_source(project_dir: str, source_id: str, relevant: bool, evidence_role: str,
                   quality_grade: str, rationale: str, supporting_excerpt: str) -> dict[str, Any]:
    if evidence_role not in {"primary", "commercial", "case-study", "counterpoint", "secondary"}:
        raise ValueError("invalid evidence role")
    if quality_grade not in {"strong", "usable-with-limits", "weak"} or not rationale.strip():
        raise ValueError("a quality grade and rationale are required")
    project = engine.require_project(project_dir)
    with project_lock(project):
        with closing(connect(project)) as conn:
            row = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if not row or row["status"] not in {"awaiting-review", "ingested"} or not row["text_path"]:
                raise ValueError("source is not eligible for review")
            body = (project / row["text_path"]).read_text(encoding="utf-8")
            if len(supporting_excerpt.strip()) < 30 or supporting_excerpt not in body:
                raise ValueError("supporting excerpt must match at least 30 characters in the source body")
            accepted = relevant and quality_grade != "weak"
            metadata = json.loads(row["metadata_json"])
            metadata["evidence_review"] = {"relevant": relevant, "role": evidence_role, "grade": quality_grade,
                                           "rationale": rationale, "excerpt": supporting_excerpt,
                                           "reviewed_at": engine.now_iso()}
            status = "ingested" if accepted else "excluded"
            conn.execute("UPDATE sources SET status=?, metadata_json=?,excluded_reason=? WHERE source_id=?",
                         (status, json.dumps(metadata), None if accepted else "evidence_review_rejected", source_id))
            conn.execute("UPDATE acquisitions SET status=? WHERE source_id=?", (status, source_id))
            conn.commit()
        engine.sync_durable_state(project)
        return {"source_id": source_id, "status": status, "review": metadata["evidence_review"]}
