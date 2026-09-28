"""Explicit, bounded file transfer across the collector/core trust boundary."""
from __future__ import annotations
import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
from ebe import network
from ebe.core import modules
from ebe.isolation import validate_local_path


def public_reference(url):
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("invalid_public_reference")
    p = urlsplit(url)
    if p.scheme not in {"https", "http"} or not p.hostname or p.username is not None or p.password is not None:
        raise ValueError("invalid_public_reference")
    engine, _, _ = modules()
    return engine.canonical_url(url)  # Strip fragments before fetching or persisting.


def read_json(path, limit=2_000_000):
    path = Path(validate_local_path(path))
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("unsafe_or_large_input")
    return json.loads(path.read_text(encoding="utf-8"))


def safe_child(root, relative):
    validate_local_path(root)
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("invalid_relative_path")
    validate_local_path(relative)
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts or ":" in relative:
        raise ValueError("path_escape")
    root = Path(root).resolve()
    candidate = root / part
    for parent in [candidate, *candidate.parents]:
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("linked_path_refused")
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("path_escape")
    return candidate


def atomic_json(path, obj):
    validate_local_path(path)
    engine, _, _ = modules()
    engine.atomic_write_text(Path(path), json.dumps(obj, ensure_ascii=False, indent=2))


def export_requests(project_dir, output, limit=20):
    validate_local_path(project_dir)
    validate_local_path(output)
    if not 1 <= limit <= 100:
        raise ValueError("invalid_batch_limit")
    engine, research, _ = modules()
    project = engine.require_project(project_dir)
    with closing(research.connect(project)) as conn:
        rows = conn.execute("SELECT source_id,url FROM sources WHERE status IN ('selected','fetch-failed') ORDER BY source_id LIMIT ?", (limit,)).fetchall()
    result = {"schema": 1, "requests": [{"id": r[0], "url": public_reference(r[1])} for r in rows]}
    output = Path(output)
    if output.exists():
        raise ValueError("output_exists")
    atomic_json(output, result)
    return {"requests": len(rows), "path": str(output), "disclosure": "URLs only; inspect before collection"}


def collect(manifest_path, output, workers=4):
    validate_local_path(manifest_path)
    validate_local_path(output)
    if not 1 <= workers <= 8:
        raise ValueError("invalid_workers")
    manifest = read_json(manifest_path)
    requests = manifest.get("requests")
    if manifest.get("schema") != 1 or not isinstance(requests, list) or not 1 <= len(requests) <= 100:
        raise ValueError("invalid_manifest")
    ids, domains = set(), {}
    for item in requests:
        if not isinstance(item, dict) or set(item) != {"id", "url"} or not re.fullmatch(r"S[0-9]{4,}", item["id"]):
            raise ValueError("invalid_request")
        if item["id"] in ids:
            raise ValueError("duplicate_request")
        ids.add(item["id"])
        item["url"] = public_reference(item["url"])
        domains.setdefault(urlsplit(item["url"]).hostname, threading.Semaphore(2))
    output = Path(output)
    if output.is_symlink():
        raise ValueError("linked_output")
    output.mkdir(parents=True, exist_ok=True)
    receipt_file = safe_child(output, "bundle.json")
    request_hash = hashlib.sha256(json.dumps(requests, sort_keys=True).encode()).hexdigest()
    receipt = {"schema": 1, "request_hash": request_hash, "results": []}
    if receipt_file.exists():
        receipt = read_json(receipt_file)
        if receipt.get("request_hash") != request_hash:
            raise ValueError("resume_manifest_changed")
        if receipt.get("schema") != 1 or not isinstance(receipt.get("results"), list):
            raise ValueError("invalid_resume_receipt")
        expected = {item["id"]: item["url"] for item in requests}
        seen = set()
        for prior in receipt["results"]:
            if (not isinstance(prior, dict) or not isinstance(prior.get("id"), str) or
                    prior["id"] in seen or prior["id"] not in expected):
                raise ValueError("resume_source_mismatch")
            prior["url"] = public_reference(prior.get("url"))
            if prior["url"] != expected[prior["id"]]:
                raise ValueError("resume_source_mismatch")
            seen.add(prior["id"])
    existing = {r["id"]: r for r in receipt["results"]}
    def download(item):
        prior = existing.get(item["id"])
        if prior and prior.get("status") == "downloaded":
            cached = safe_child(output, prior["file"])
            try:
                if cached.stat().st_size <= 15*1024*1024 and hashlib.sha256(cached.read_bytes()).hexdigest() == prior["sha256"]:
                    return prior
            except FileNotFoundError:
                pass  # Interrupted/removed cache: fetch this item again.
        try:
            with domains[urlsplit(item["url"]).hostname]:
                body, mime, final = network.fetch(item["url"])
            digest = hashlib.sha256(body).hexdigest()
            suffix = {"application/pdf": ".pdf", "text/html": ".html", "text/plain": ".txt",
                      "application/json": ".json", "text/markdown": ".md"}.get(mime)
            if suffix is None:
                raise ValueError("unsupported_mime")
            name = digest + suffix
            engine, _, _ = modules()
            engine.atomic_write_bytes(safe_child(output, name), body)
            return {**item, "status": "downloaded", "file": name, "sha256": digest, "content_type": mime}
        except Exception as exc:
            return {**item, "status": "failed", "error": type(exc).__name__}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(download, item) for item in requests]):
            result = future.result()
            existing[result["id"]] = result
            receipt["results"] = [existing[k] for k in sorted(existing)]
            atomic_json(receipt_file, receipt)
    return {"downloaded": sum(r["status"] == "downloaded" for r in receipt["results"]),
            "failed": sum(r["status"] == "failed" for r in receipt["results"]), "bundle": str(receipt_file)}


def import_bundle(project_dir, bundle_path):
    validate_local_path(project_dir)
    validate_local_path(bundle_path)
    engine, research, pipeline = modules()
    project = engine.require_project(project_dir)
    bundle_path = Path(bundle_path)
    manifest = read_json(bundle_path)
    results = manifest.get("results")
    if manifest.get("schema") != 1 or not isinstance(results, list) or len(results) > 100:
        raise ValueError("invalid_bundle")
    prepared, seen, total_bytes = [], set(), 0
    # Validate the entire manifest before any write. Never trust collector paths or IDs.
    with closing(engine.connect(project)) as conn:
        for item in results:
            if item.get("status") != "downloaded":
                continue
            sid = item.get("id")
            if not isinstance(sid, str) or not re.fullmatch(r"S[0-9]{4,}", sid) or sid in seen:
                raise ValueError("invalid_source_id")
            seen.add(sid)
            item["url"] = public_reference(item["url"])
            source = conn.execute("SELECT url,status FROM sources WHERE source_id=?", (sid,)).fetchone()
            if not source or source["url"] != item["url"]:
                raise ValueError("bundle_source_mismatch")
            if source["status"] not in {"selected", "fetch-failed", "ingested", "awaiting-review", "excluded"}:
                raise ValueError("source_must_be_selected")
            path = safe_child(bundle_path.parent, item["file"])
            size = path.stat().st_size
            if size > engine.MAX_FETCH_BYTES:
                raise ValueError("source_too_large")
            remaining = 64 * 1024 * 1024 - total_bytes
            if size > remaining:
                raise ValueError("bundle_memory_budget_exceeded_split_batch")
            # Bound the read too: the file can grow after stat().
            with path.open("rb") as handle:
                body = handle.read(min(engine.MAX_FETCH_BYTES, remaining) + 1)
            if len(body) > engine.MAX_FETCH_BYTES:
                raise ValueError("source_too_large")
            total_bytes += len(body)
            if total_bytes > 64 * 1024 * 1024:
                raise ValueError("bundle_memory_budget_exceeded_split_batch")
            if hashlib.sha256(body).hexdigest() != item["sha256"]:
                raise ValueError("bundle_hash_mismatch")
            if item["content_type"] not in {"application/pdf", "text/html", "text/plain", "application/json", "text/markdown"}:
                raise ValueError("unsupported_mime")
            prepared.append((item, body, source["status"]))
    imported = []
    from storage import batch_durable_sync
    with pipeline.project_lock(project), batch_durable_sync(project):
        for item, body, status in prepared:
            if status in {"ingested", "awaiting-review", "excluded"}:
                continue
            suffix = {"application/pdf": ".pdf", "text/html": ".html", "application/json": ".json"}.get(item["content_type"], ".txt")
            relative = f"sources/raw/{item['id']}-{item['sha256'][:12]}{suffix}"
            raw = safe_child(project, relative)
            engine.atomic_write_bytes(raw, body)
            with closing(engine.connect(project)) as conn:
                record = engine.ingest_saved_file(project, conn, item["id"], raw, item["content_type"], 7000, 500)
                conn.commit()
            engine.sync_durable_state(project, [relative] + ([record["text_path"]] if record.get("text_path") else []))
            review = research.assess_source(project, item["id"])
            with closing(research.connect(project)) as conn:
                conn.execute("UPDATE acquisitions SET status=?,attempts=attempts+1,result_json=? WHERE source_id=?",
                             (review["status"], json.dumps(review), item["id"]))
                if review["status"] == "awaiting-review":
                    prior = conn.execute("SELECT batch_id,source_ids FROM acquisition_batches ORDER BY batch_id DESC LIMIT 1").fetchone()
                    ids = json.loads(prior[1]) if prior else []
                    if prior and len(ids) < 20:
                        if item["id"] not in ids:
                            conn.execute("UPDATE acquisition_batches SET source_ids=? WHERE batch_id=?", (json.dumps(ids + [item["id"]]), prior[0]))
                    else:
                        conn.execute("INSERT INTO acquisition_batches(created_at,source_ids) VALUES (?,?)", (engine.now_iso(), json.dumps([item["id"]])))
                conn.commit()
            imported.append(review)
        engine.sync_durable_state(project)
    return {"imported": imported, "skipped_existing": len(prepared) - len(imported)}
