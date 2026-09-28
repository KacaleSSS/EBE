#!/usr/bin/env python3
"""Persistent research and ebook workspace for the Topic to Ebook plugin."""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4
from xml.etree import ElementTree
from ebe.isolation import validate_local_path

from storage import (
    PROJECT_MARKER,
    atomic_copy,
    atomic_write_bytes,
    atomic_write_text,
    guarded_remove_project,
    is_relative_to,
    local_path,
    mirror_project_path,
    restore_project_from_mirror,
    sha256_file,
    sync_project_mirror,
)


SCHEMA_VERSION = 2
MAX_FETCH_BYTES = 15 * 1024 * 1024
MAX_PDF_TEXT_BYTES = 15 * 1024 * 1024
DEFAULT_CHUNK_CHARS = 7000
DEFAULT_OVERLAP_CHARS = 500
MIN_QUALIFIED_SOURCES = 120
MIN_SOURCES_PER_DIMENSION = 3
MIN_SOURCES_PER_HIGH_IMPACT_CLAIM = 2
SATURATION_BATCH_SIZE = 20
MAX_NEW_CLAIM_RATE = 0.05
FINALIZATION_TTL_MINUTES = 60
RESEARCH_DIMENSIONS = (
    "audience",
    "pain",
    "desired-result",
    "alternatives",
    "willingness-to-pay",
    "objections",
    "mechanism",
    "creator-edge",
    "risks",
    "implementation",
)
GATE_ARTIFACT_TYPES = {
    "uvz": "uvz-analysis",
    "outline": "ebook-outline",
    "final": "quality-report",
}
ARTIFACT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
SOURCE_ID_RE = re.compile(r"^S\d{4,}$")
CITATION_RE = re.compile(r"\[(S\d{4,})\]")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def safe_filename(value: str, fallback: str = "source") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return value[:100] or fallback


def canonical_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL must use http or https")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials or missing hostname")
    for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        name = re.sub(r"[^a-z0-9]", "", key.lower())
        if (any(word in name for word in ("token", "secret", "password", "signature", "credential")) or
                name in {"key", "apikey", "accesskey", "auth", "authorization", "sig", "jwt", "pwd",
                         "awsaccesskeyid", "googleaccessid", "policy"} or
                key.lower().startswith(("x-amz-", "x-goog-", "x-oss-"))):
            raise ValueError("sensitive_url_query")
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, "")
    )


def validate_public_url(url: str) -> str:
    canonical = canonical_url(url)
    parsed = urllib.parse.urlsplit(canonical)
    if parsed.username or parsed.password:
        raise ValueError("URL credentials are not allowed")
    if not parsed.hostname:
        raise ValueError("URL must contain a hostname")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("URL must use the default http or https port")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except socket.gaierror as exc:
        raise ValueError(f"URL hostname cannot be resolved: {parsed.hostname}") from exc
    if not addresses:
        raise ValueError("URL hostname resolved to no addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError(f"URL resolves to a non-public address: {address}")
    return canonical


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        validated = validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, validated)


class VisibleHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1
        elif tag.lower() in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag.lower() in {"p", "li", "h1", "h2", "h3", "h4", "tr", "div"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


def normalize_text(text: str) -> str:
    text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_docx(path: Path) -> str:
    validate_local_path(path)
    with zipfile.ZipFile(path) as archive:
        total_uncompressed = sum(item.file_size for item in archive.infolist())
        if total_uncompressed > 50 * 1024 * 1024:
            raise ValueError("DOCX expanded content exceeds 50 MiB")
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    parts: list[str] = []
    for element in root.iter():
        if element.tag.endswith("}t") and element.text:
            parts.append(element.text)
        elif element.tag.endswith("}p"):
            parts.append("\n")
    return normalize_text(" ".join(parts))


def extract_text(path: Path, content_type: str | None = None) -> tuple[str, str]:
    path = local_path(path)
    suffix = path.suffix.lower()
    content_type = (content_type or "").lower()
    if suffix == ".docx":
        return extract_docx(path), "docx-xml"
    if suffix == ".pdf" or "application/pdf" in content_type:
        executable = shutil.which("pdftotext")
        if not executable:
            return "", "pdf-needs-pdftotext"
        # CPU/address-space/disk isolation belongs to the offline Docker worker.
        # Parent memory stays bounded even for maliciously expansive PDF output.
        with tempfile.TemporaryDirectory(prefix="ebe-pdf-") as directory:
            output = Path(directory) / "body.txt"
            environment = {k: os.environ[k] for k in ("SystemRoot", "WINDIR") if k in os.environ}
            environment.update(TMP=directory, TEMP=directory, TMPDIR=directory)
            try:
                result = subprocess.run(
                    [executable, "-layout", str(path.resolve()), str(output)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=environment, cwd=directory, check=False, timeout=120,
                )
            except subprocess.TimeoutExpired:
                return "", "pdf-extraction-timeout"
            if result.returncode != 0 or not output.is_file():
                return "", "pdf-extraction-failed"
            with output.open("rb") as handle:
                raw = handle.read(MAX_PDF_TEXT_BYTES + 1)
            if len(raw) > MAX_PDF_TEXT_BYTES:
                return "", "pdf-extraction-too-large"
        return normalize_text(raw.decode("utf-8", errors="replace")), "pdftotext"
    raw = path.read_bytes()
    decoded = raw.decode("utf-8", errors="replace")
    if suffix in {".html", ".htm"} or "text/html" in content_type:
        parser = VisibleHTMLParser()
        parser.feed(decoded)
        return normalize_text(parser.text()), "html-parser"
    if suffix in {".txt", ".md", ".markdown", ".csv", ".json", ".xml"} or content_type.startswith("text/"):
        return normalize_text(decoded), "text"
    return "", "unsupported"


def chunk_text(
    text: str,
    size: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_OVERLAP_CHARS,
) -> Iterable[tuple[int, int, str]]:
    if size <= overlap or overlap < 0:
        raise ValueError("chunk size must be greater than overlap")
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("\n", start + size // 2, end), text.rfind(". ", start + size // 2, end))
            if boundary > start:
                end = boundary + 1
        body = text[start:end].strip()
        if body:
            yield start, end, body
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)


def db_path(project: Path) -> Path:
    validate_local_path(project)
    return project / "state.sqlite3"


def connect(project: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path(project), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS sources (
            source_id TEXT PRIMARY KEY,
            url TEXT UNIQUE,
            title TEXT NOT NULL,
            publisher TEXT,
            published_at TEXT,
            retrieved_at TEXT,
            source_type TEXT NOT NULL,
            status TEXT NOT NULL,
            search_query TEXT,
            selected_reason TEXT,
            excluded_reason TEXT,
            content_type TEXT,
            sha256 TEXT,
            text_sha256 TEXT,
            raw_path TEXT,
            text_path TEXT,
            extractor TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            char_start INTEGER NOT NULL,
            char_end INTEGER NOT NULL,
            body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            artifact_type TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(artifact_type, version)
        );
        CREATE TABLE IF NOT EXISTS decisions (
            decision_id TEXT PRIMARY KEY,
            gate TEXT NOT NULL,
            status TEXT NOT NULL,
            artifact_id TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS retrieval_runs (
            retrieval_id TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS finalizations (
            finalization_id TEXT PRIMARY KEY,
            release_path TEXT NOT NULL,
            ebook_sha256 TEXT NOT NULL,
            token_sha256 TEXT NOT NULL,
            prepared_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL
        );
        """
    )
    source_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(sources)").fetchall()
    }
    if "text_sha256" not in source_columns:
        connection.execute("ALTER TABLE sources ADD COLUMN text_sha256 TEXT")
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, source_id UNINDEXED, body)"
        )
    except sqlite3.OperationalError:
        pass
    connection.commit()


def read_project_metadata(project: Path) -> dict[str, Any]:
    validate_local_path(project)
    metadata = json.loads((project / "project.json").read_text(encoding="utf-8"))
    if metadata.get("mirror_root"):
        local_path(metadata["mirror_root"])
    for value in metadata.get("import_roots") or []:
        local_path(value)
    return metadata


def sync_durable_state(project: Path, relative_paths: Iterable[str | Path] = ()) -> dict[str, object]:
    return sync_project_mirror(project, relative_paths)


def allowed_import_roots(project: Path) -> list[Path]:
    validate_local_path(project)
    metadata = read_project_metadata(project)
    configured = metadata.get("import_roots") or [str(project / "input")]
    paths = [local_path(item) for item in configured]
    return [path.resolve() for path in paths]


def require_project(project_dir: str | Path) -> Path:
    project = local_path(project_dir).resolve()
    if not (project / "project.json").is_file() or not db_path(project).is_file():
        raise ValueError(f"not a Topic to Ebook project: {project}")
    return project


def log_event(connection: sqlite3.Connection, event_type: str, payload: Any) -> None:
    connection.execute(
        "INSERT INTO events(event_type, payload_json, created_at) VALUES (?, ?, ?)",
        (event_type, json_text(payload), now_iso()),
    )


def init_project(
    root: str | Path,
    project_slug: str,
    topic: str,
    language: str = "zh-CN",
    audience_hint: str | None = None,
    mirror_root: str | Path | None = None,
    import_roots: list[str] | None = None,
    allow_single_copy: bool = False,
) -> dict[str, Any]:
    if not PROJECT_SLUG_RE.fullmatch(project_slug):
        raise ValueError("project_slug must use 2-63 lowercase letters, digits, or hyphens")
    if not topic.strip():
        raise ValueError("topic is required")
    root = local_path(root)
    mirror_root = local_path(mirror_root) if mirror_root else None
    import_roots = [local_path(item) for item in import_roots] if import_roots else None
    root_path = root.resolve()
    project = root_path / project_slug
    if mirror_root is None and not allow_single_copy:
        raise ValueError(
            "mirror_root is required; use allow_single_copy only for disposable synthetic tests"
        )
    mirror = mirror_project_path(project, mirror_root, project_slug)
    project.mkdir(parents=True, exist_ok=True)
    for relative in (
        "input",
        "sources/raw",
        "sources/text",
        "artifacts",
        "outputs",
        "logs",
    ):
        (project / relative).mkdir(parents=True, exist_ok=True)
    metadata_path = project / "project.json"
    if metadata_path.exists():
        current = read_project_metadata(project)
        if current.get("topic") != topic.strip():
            raise ValueError("project already exists with a different topic")
        if mirror_root:
            requested_mirror = str(mirror_root.resolve())
            if current.get("mirror_root") and current.get("mirror_root") != requested_mirror:
                raise ValueError("project already exists with a different mirror_root")
            current["mirror_root"] = requested_mirror
        if not current.get("project_uuid"):
            current["project_uuid"] = str(uuid4())
        current["schema_version"] = SCHEMA_VERSION
        current.setdefault("mirror_root", None)
        current.setdefault("import_roots", [str(project / "input")])
        current["storage_policy"] = "mirrored-durable" if current.get("mirror_root") else "single-copy"
        atomic_write_text(metadata_path, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
        atomic_write_text(
            project / PROJECT_MARKER,
            json.dumps(
                {"project_uuid": current["project_uuid"], "project_id": project_slug},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    else:
        project_uuid = str(uuid4())
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_slug,
            "project_uuid": project_uuid,
            "topic": topic.strip(),
            "language": language,
            "audience_hint": audience_hint,
            "mirror_root": str(mirror_root.resolve()) if mirror_root else None,
            "import_roots": [
                str(local_path(item).resolve()) for item in (import_roots or [str(project / "input")])
            ],
            "storage_policy": "mirrored-durable" if mirror else "single-copy",
            "created_at": now_iso(),
        }
        atomic_write_text(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        atomic_write_text(
            project / PROJECT_MARKER,
            json.dumps({"project_uuid": project_uuid, "project_id": project_slug}, ensure_ascii=False, indent=2)
            + "\n",
        )
        atomic_write_text(project / "input" / "topic.md", f"# Topic\n\n{topic.strip()}\n")
    with closing(connect(project)) as connection:
        init_schema(connection)
        log_event(connection, "project_initialized", {"topic": topic.strip()})
        connection.commit()
    sync_durable_state(project, ["input/topic.md"])
    return project_status(project)


def next_source_id(connection: sqlite3.Connection) -> str:
    rows = connection.execute("SELECT source_id FROM sources ORDER BY source_id").fetchall()
    maximum = max((int(row[0][1:]) for row in rows if SOURCE_ID_RE.fullmatch(row[0])), default=0)
    return f"S{maximum + 1:04d}"


def register_sources(project_dir: str | Path, sources: list[dict[str, Any]]) -> dict[str, Any]:
    project = require_project(project_dir)
    registered: list[dict[str, Any]] = []
    with closing(connect(project)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for item in sources:
            url = canonical_url(item["url"]) if item.get("url") else None
            if url:
                existing = connection.execute("SELECT * FROM sources WHERE url = ?", (url,)).fetchone()
                if existing:
                    registered.append({"source_id": existing["source_id"], "url": url, "duplicate": True})
                    continue
            source_id = next_source_id(connection)
            status = item.get("status", "candidate")
            if status not in {"candidate", "selected", "excluded"}:
                raise ValueError("source status must be candidate, selected, or excluded")
            connection.execute(
                """
                INSERT INTO sources(
                    source_id, url, title, publisher, published_at, retrieved_at,
                    source_type, status, search_query, selected_reason, excluded_reason, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    url,
                    item.get("title") or url or source_id,
                    item.get("publisher"),
                    item.get("published_at"),
                    item.get("retrieved_at") or now_iso(),
                    item.get("source_type", "unknown"),
                    status,
                    item.get("search_query"),
                    item.get("selected_reason"),
                    item.get("excluded_reason"),
                    json_text(item.get("metadata", {})),
                ),
            )
            registered.append({"source_id": source_id, "url": url, "duplicate": False})
        log_event(connection, "sources_registered", {"count": len(registered)})
        connection.commit()
    durability = sync_durable_state(project)
    return {"project": str(project), "sources": registered, "durability": durability}


def replace_source_chunks(
    connection: sqlite3.Connection,
    source_id: str,
    text: str,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> int:
    connection.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
    try:
        connection.execute("DELETE FROM chunks_fts WHERE source_id = ?", (source_id,))
    except sqlite3.OperationalError:
        pass
    count = 0
    for count, (start, end, body) in enumerate(chunk_text(text, chunk_chars, overlap_chars), start=1):
        chunk_id = f"{source_id}-C{count:04d}"
        connection.execute(
            "INSERT INTO chunks(chunk_id, source_id, ordinal, char_start, char_end, body) VALUES (?, ?, ?, ?, ?, ?)",
            (chunk_id, source_id, count, start, end, body),
        )
        try:
            connection.execute(
                "INSERT INTO chunks_fts(chunk_id, source_id, body) VALUES (?, ?, ?)",
                (chunk_id, source_id, body),
            )
        except sqlite3.OperationalError:
            pass
    return count


def ingest_saved_file(
    project: Path,
    connection: sqlite3.Connection,
    source_id: str,
    raw_path: Path,
    content_type: str | None,
    chunk_chars: int,
    overlap_chars: int,
) -> dict[str, Any]:
    validate_local_path(project)
    validate_local_path(raw_path)
    text, extractor = extract_text(raw_path, content_type)
    digest = sha256_file(raw_path)
    relative_raw = raw_path.relative_to(project).as_posix()
    if not text:
        connection.execute(
            "UPDATE sources SET status = ?, content_type = ?, sha256 = ?, raw_path = ?, extractor = ? WHERE source_id = ?",
            ("unreadable", content_type, digest, relative_raw, extractor, source_id),
        )
        return {
            "source_id": source_id,
            "status": "unreadable",
            "extractor": extractor,
            "chunks": 0,
            "raw_path": relative_raw,
            "text_path": None,
        }
    text_path = project / "sources" / "text" / f"{source_id}.txt"
    atomic_write_text(text_path, text)
    text_digest = sha256_text(text)
    chunk_count = replace_source_chunks(connection, source_id, text, chunk_chars, overlap_chars)
    connection.execute(
        """
        UPDATE sources SET status = ?, content_type = ?, sha256 = ?, text_sha256 = ?, raw_path = ?, text_path = ?, extractor = ?
        WHERE source_id = ?
        """,
        (
            "ingested",
            content_type,
            digest,
            text_digest,
            relative_raw,
            text_path.relative_to(project).as_posix(),
            extractor,
            source_id,
        ),
    )
    return {
        "source_id": source_id,
        "status": "ingested",
        "extractor": extractor,
        "characters": len(text),
        "chunks": chunk_count,
        "sha256": digest,
        "text_sha256": text_digest,
        "raw_path": relative_raw,
        "text_path": text_path.relative_to(project).as_posix(),
    }


def ingest_url(
    project_dir: str | Path,
    source_id: str,
    timeout_seconds: int = 25,
    max_bytes: int = MAX_FETCH_BYTES,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> dict[str, Any]:
    project = require_project(project_dir)
    with closing(connect(project)) as connection:
        row = connection.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
        if not row or not row["url"]:
            raise ValueError("source_id must reference a registered URL")
        if row["status"] not in {"selected", "ingested"}:
            raise ValueError("source must be explicitly selected before ingestion")
        try:
            downloaded = download_url(row["url"], max_bytes, timeout_seconds)
        except Exception as exc:
            connection.execute("UPDATE sources SET status = ? WHERE source_id = ?", ("fetch-failed", source_id))
            log_event(connection, "source_fetch_failed", {"source_id": source_id, "error": type(exc).__name__})
            connection.commit()
            failed: dict[str, Any] = {
                "source_id": source_id,
                "status": "fetch-failed",
                "error": type(exc).__name__,
            }
            failed["durability"] = sync_durable_state(project)
            return failed
    return commit_download(project, source_id, downloaded, chunk_chars, overlap_chars)


def download_url(url: str, max_bytes: int = MAX_FETCH_BYTES,
                 timeout: int = 25) -> tuple[bytes, str, str]:
    """Network only: no project files or SQLite connections in workers."""
    from ebe.network import fetch

    downloaded = fetch(canonical_url(url), max_bytes=max_bytes, timeout=timeout)
    canonical_url(downloaded[2])
    if len(downloaded[0]) > max_bytes:
        raise ValueError("source_too_large")
    return downloaded


def commit_download(project_dir: str | Path, source_id: str,
                    downloaded: tuple[bytes, str, str],
                    chunk_chars: int = DEFAULT_CHUNK_CHARS,
                    overlap_chars: int = DEFAULT_OVERLAP_CHARS) -> dict[str, Any]:
    """Parse and commit on the importing thread, using its own connection."""
    project = require_project(project_dir)
    data, content_type, final_url = downloaded
    with closing(connect(project)) as connection:
        row = connection.execute("SELECT status FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if not row or row["status"] not in {"selected", "ingested"}:
            raise ValueError("source must be explicitly selected before ingestion")
        suffix = Path(urllib.parse.urlsplit(final_url).path).suffix.lower()
        if not suffix:
            suffix = mimetypes.guess_extension(content_type) or ".bin"
        suffix = "." + safe_filename(suffix.lstrip("."), "bin")
        snapshot_hash = sha256_bytes(data)
        raw_path = project / "sources" / "raw" / f"{source_id}-{snapshot_hash[:12]}{suffix}"
        if not raw_path.exists():
            atomic_write_bytes(raw_path, data)
        result = ingest_saved_file(
            project, connection, source_id, raw_path, content_type, chunk_chars, overlap_chars
        )
        log_event(connection, "source_ingested", result)
        connection.commit()
    durability = sync_durable_state(
        project,
        [item for item in (result.get("raw_path"), result.get("text_path")) if item],
    )
    result["durability"] = durability
    return result


def ingest_file(
    project_dir: str | Path,
    file_path: str | Path,
    title: str | None = None,
    source_type: str = "user-supplied",
    max_bytes: int = MAX_FETCH_BYTES,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> dict[str, Any]:
    validate_local_path(project_dir)
    source_path = local_path(file_path)
    project = require_project(project_dir)
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise ValueError(f"file does not exist: {source_path}")
    roots = allowed_import_roots(project)
    if not any(is_relative_to(source_path, root) for root in roots):
        raise ValueError(
            "file_path is outside the project's approved import roots; copy it into input/ or configure an approved root"
        )
    if source_path.stat().st_size > max_bytes:
        raise ValueError(f"source exceeds {max_bytes} bytes")
    source_digest = sha256_file(source_path)
    with closing(connect(project)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT source_id, raw_path, text_path, text_sha256 FROM sources WHERE status = 'ingested' AND sha256 = ? LIMIT 1",
            (source_digest,),
        ).fetchone()
        if existing:
            connection.rollback()
            durability = sync_durable_state(
                project,
                [item for item in (existing["raw_path"], existing["text_path"]) if item],
            )
            return {
                "source_id": existing["source_id"],
                "status": "ingested",
                "sha256": source_digest,
                "text_sha256": existing["text_sha256"],
                "duplicate": True,
                "durability": durability,
            }
        source_id = next_source_id(connection)
        connection.execute(
            "INSERT INTO sources(source_id, url, title, source_type, status, retrieved_at, metadata_json) VALUES (?, NULL, ?, ?, ?, ?, '{}')",
            (source_id, title or source_path.name, source_type, "selected", now_iso()),
        )
        raw_path = (
            project
            / "sources"
            / "raw"
            / f"{source_id}-{source_digest[:12]}-{safe_filename(source_path.name)}"
        )
        atomic_copy(source_path, raw_path)
        result = ingest_saved_file(
            project,
            connection,
            source_id,
            raw_path,
            mimetypes.guess_type(source_path.name)[0],
            chunk_chars,
            overlap_chars,
        )
        log_event(connection, "local_source_ingested", result)
        connection.commit()
    durability = sync_durable_state(
        project,
        [item for item in (result.get("raw_path"), result.get("text_path")) if item],
    )
    result["durability"] = durability
    result["duplicate"] = False
    return result


def search_terms(query: str) -> list[str]:
    lowered = query.lower()
    terms = re.findall(r"[a-z0-9][a-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", lowered)
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", term):
            expanded.extend(term[index : index + 2] for index in range(len(term) - 1))
    return list(dict.fromkeys(expanded))


def search_corpus(project_dir: str | Path, query: str, limit: int = 8) -> dict[str, Any]:
    project = require_project(project_dir)
    terms = search_terms(query)
    if not terms:
        raise ValueError("query must contain searchable terms")
    requested_limit = max(1, min(limit, 50))
    with closing(connect(project)) as connection:
        candidates: dict[str, tuple[float, sqlite3.Row]] = {}
        try:
            fts_query = " OR ".join(f'"{term}"' for term in terms)
            fts_rows = connection.execute(
                """
                SELECT c.chunk_id, c.source_id, c.body, s.title, s.url, s.source_type,
                       bm25(chunks_fts, 0.0, 0.0, 1.0) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
                JOIN sources s ON s.source_id = c.source_id
                WHERE chunks_fts MATCH ? AND s.status = 'ingested'
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, requested_limit * 8),
            ).fetchall()
            for row in fts_rows:
                candidates[row["chunk_id"]] = (-float(row["rank"]), row)
        except sqlite3.OperationalError:
            pass

        lexical_rows = connection.execute(
            """
            SELECT c.chunk_id, c.source_id, c.body, s.title, s.url, s.source_type
            FROM chunks c JOIN sources s ON s.source_id = c.source_id
            WHERE s.status = 'ingested'
            """
        ).fetchall()
        for row in lexical_rows:
            body = row["body"].lower()
            title = row["title"].lower()
            count_score = sum(body.count(term) + 3 * title.count(term) for term in terms)
            coverage = sum(1 for term in terms if term in body or term in title)
            if count_score:
                lexical_score = count_score + coverage * 0.5
                prior = candidates.get(row["chunk_id"])
                candidates[row["chunk_id"]] = (lexical_score + (prior[0] if prior else 0.0), row)

        ranked = sorted(candidates.values(), key=lambda item: (-item[0], item[1]["chunk_id"]))
        results = []
        per_source: dict[str, int] = {}
        for score, row in ranked:
            if per_source.get(row["source_id"], 0) >= 2:
                continue
            lower = row["body"].lower()
            positions = [lower.find(term) for term in terms if lower.find(term) >= 0]
            start = max(0, (min(positions) if positions else 0) - 240)
            results.append(
                {
                    "chunk_id": row["chunk_id"],
                    "source_id": row["source_id"],
                    "title": row["title"],
                    "url": row["url"],
                    "source_type": row["source_type"],
                    "score": round(score, 4),
                    "snippet": row["body"][start : start + 1200],
                }
            )
            per_source[row["source_id"]] = per_source.get(row["source_id"], 0) + 1
            if len(results) >= requested_limit:
                break
        retrieval_id = f"R-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(5)}"
        connection.execute(
            "INSERT INTO retrieval_runs(retrieval_id, query, result_json, created_at) VALUES (?, ?, ?, ?)",
            (retrieval_id, query, json_text(results), now_iso()),
        )
        log_event(connection, "corpus_searched", {"retrieval_id": retrieval_id, "results": len(results)})
        connection.commit()
    sync_durable_state(project)
    return {"retrieval_id": retrieval_id, "query": query, "terms": terms, "results": results}


def next_artifact_version(connection: sqlite3.Connection, artifact_type: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM artifacts WHERE artifact_type = ?",
        (artifact_type,),
    ).fetchone()
    return int(row["version"]) + 1


def save_artifact(
    project_dir: str | Path,
    artifact_type: str,
    content: str,
    status: str = "draft",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project = require_project(project_dir)
    if not ARTIFACT_NAME_RE.fullmatch(artifact_type):
        raise ValueError("artifact_type must be lower-case hyphen form")
    metadata = metadata or {}
    if status not in {"draft", "ready", "approved", "rejected", "superseded"}:
        raise ValueError("invalid artifact status")
    with closing(connect(project)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        version = next_artifact_version(connection, artifact_type)
        stored_content = content.rstrip() + "\n"
        digest = sha256_text(stored_content)
        artifact_id = f"A-{artifact_type}-{version:03d}-{digest[:8]}"
        artifact_dir = project / "artifacts" / artifact_type
        artifact_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".json" if metadata.get("format") == "json" else ".md"
        artifact_path = artifact_dir / f"v{version:03d}-{digest[:8]}{suffix}"
        atomic_write_text(artifact_path, stored_content)
        connection.execute(
            """
            INSERT INTO artifacts(artifact_id, artifact_type, version, status, file_path, sha256, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                artifact_type,
                version,
                status,
                artifact_path.relative_to(project).as_posix(),
                digest,
                now_iso(),
                json_text(metadata),
            ),
        )
        log_event(connection, "artifact_saved", {"artifact_id": artifact_id, "status": status})
        connection.commit()
    durability = sync_durable_state(project, [artifact_path.relative_to(project)])
    return {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "version": version,
        "status": status,
        "path": str(artifact_path),
        "sha256": digest,
        "durability": durability,
    }


def artifact_rows(connection: sqlite3.Connection, artifact_type: str | None = None) -> list[sqlite3.Row]:
    if artifact_type:
        return connection.execute(
            "SELECT * FROM artifacts WHERE artifact_type = ? ORDER BY version DESC", (artifact_type,)
        ).fetchall()
    return connection.execute("SELECT * FROM artifacts ORDER BY created_at DESC").fetchall()


def latest_artifact(project: Path, connection: sqlite3.Connection, artifact_type: str) -> tuple[sqlite3.Row, str] | None:
    row = connection.execute(
        "SELECT * FROM artifacts WHERE artifact_type = ? ORDER BY version DESC LIMIT 1",
        (artifact_type,),
    ).fetchone()
    if not row:
        return None
    path = project / validate_local_path(row["file_path"])
    return row, path.read_text(encoding="utf-8")


def research_readiness_from_connection(connection: sqlite3.Connection) -> dict[str, Any]:
    qualified_rows = connection.execute(
        "SELECT source_id, sha256 FROM sources WHERE status = 'ingested' AND sha256 IS NOT NULL"
    ).fetchall()
    source_hashes = {row["source_id"]: row["sha256"] for row in qualified_rows}
    qualified_sources = len(set(source_hashes.values()))
    ingested_records = int(
        connection.execute("SELECT COUNT(*) FROM sources WHERE status = 'ingested'").fetchone()[0]
    )
    row = connection.execute(
        "SELECT * FROM artifacts WHERE artifact_type = 'saturation-report' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    metadata = json.loads(row["metadata_json"] or "{}") if row else {}
    failures: list[str] = []

    def normalized_source_ids(value: Any, label: str) -> list[str]:
        if not isinstance(value, list):
            return []
        if any(not isinstance(item, str) for item in value):
            failures.append(f"{label} source_ids must contain only strings")
        return list(dict.fromkeys(item for item in value if isinstance(item, str)))

    if qualified_sources < MIN_QUALIFIED_SOURCES:
        failures.append(
            f"qualified sources {qualified_sources}/{MIN_QUALIFIED_SOURCES}; only unique successfully ingested sources count"
        )

    raw_coverage = metadata.get("coverage")
    coverage: dict[str, Any] = raw_coverage if isinstance(raw_coverage, dict) else {}
    dimension_counts: dict[str, int] = {}
    coverage_source_ids: dict[str, list[str]] = {}
    for dimension in RESEARCH_DIMENSIONS:
        value = coverage.get(dimension, 0)
        source_ids = normalized_source_ids(value, f"{dimension} coverage")
        invalid = sorted(item for item in source_ids if item not in source_hashes)
        if invalid:
            failures.append(f"{dimension} coverage references unqualified sources: {', '.join(invalid)}")
        valid_ids = [item for item in source_ids if item in source_hashes]
        count = len({source_hashes[item] for item in valid_ids})
        dimension_counts[dimension] = count
        coverage_source_ids[dimension] = valid_ids
    undercovered = [
        dimension for dimension, count in dimension_counts.items() if count < MIN_SOURCES_PER_DIMENSION
    ]
    if undercovered:
        failures.append(
            "research dimensions below three independent sources: " + ", ".join(undercovered)
        )

    high_impact_claims = metadata.get("high_impact_claims")
    claims_total = len(high_impact_claims) if isinstance(high_impact_claims, list) else 0
    claims_supported = 0
    if not isinstance(high_impact_claims, list) or not high_impact_claims:
        failures.append("saturation report must include a non-empty high_impact_claims list")
        high_impact_claims = []
    for index, claim in enumerate(high_impact_claims, start=1):
        if not isinstance(claim, dict) or not claim.get("claim_id"):
            failures.append(f"high impact claim {index} is invalid")
            continue
        source_ids = normalized_source_ids(
            claim.get("source_ids") or [], f"claim {claim['claim_id']}"
        )
        valid_ids = [item for item in source_ids if item in source_hashes]
        invalid = sorted(item for item in source_ids if item not in source_hashes)
        independent = len({source_hashes[item] for item in valid_ids})
        if invalid:
            failures.append(f"claim {claim['claim_id']} references unqualified sources: {', '.join(invalid)}")
        if independent < MIN_SOURCES_PER_HIGH_IMPACT_CLAIM:
            failures.append(
                f"claim {claim['claim_id']} has {independent}/{MIN_SOURCES_PER_HIGH_IMPACT_CLAIM} independent sources"
            )
            continue
        risk = claim.get("risk", "normal")
        authoritative = set(
            normalized_source_ids(
                claim.get("authoritative_source_ids") or [],
                f"claim {claim['claim_id']} authoritative",
            )
        )
        if risk in {"high", "rapidly-changing"} and not authoritative.intersection(valid_ids):
            failures.append(f"claim {claim['claim_id']} requires an authoritative current source")
            continue
        claims_supported += 1

    recent_batches = metadata.get("recent_batches")
    batch_results: list[dict[str, Any]] = []
    if not isinstance(recent_batches, list) or len(recent_batches) < 2:
        failures.append("saturation report needs two consecutive recent batches")
    else:
        recent_source_sets: list[set[str]] = []
        for index, batch in enumerate(recent_batches[-2:], start=1):
            if not isinstance(batch, dict):
                failures.append(f"recent batch {index} is invalid")
                continue
            batch_source_ids = normalized_source_ids(
                batch.get("source_ids") or [], f"recent batch {index}"
            )
            invalid_sources = sorted(item for item in batch_source_ids if item not in source_hashes)
            batch_size = len({source_hashes[item] for item in batch_source_ids if item in source_hashes})
            new_claims = batch.get("new_material_claims", 0)
            claims_before = batch.get("material_claims_before", 0)
            valid_numbers = all(
                isinstance(value, int) and value >= 0
                for value in (new_claims, claims_before)
            )
            if not valid_numbers:
                failures.append(f"recent batch {index} has invalid counts")
                continue
            if invalid_sources:
                failures.append(
                    f"recent batch {index} references unqualified sources: {', '.join(invalid_sources)}"
                )
            recent_source_sets.append(set(batch_source_ids))
            rate = new_claims / max(claims_before, 1)
            stable = not batch.get("uvz_changed", False) and not batch.get("promise_changed", False)
            batch_results.append(
                {
                    "source_ids": batch_source_ids,
                    "batch_size": batch_size,
                    "new_material_claims": new_claims,
                    "material_claims_before": claims_before,
                    "new_material_claim_rate": round(rate, 4),
                    "stable": stable,
                }
            )
            if batch_size < SATURATION_BATCH_SIZE:
                failures.append(
                    f"recent batch {index} contains {batch_size}/{SATURATION_BATCH_SIZE} sources"
                )
            if rate > MAX_NEW_CLAIM_RATE:
                failures.append(
                    f"recent batch {index} new material claim rate {rate:.1%} exceeds {MAX_NEW_CLAIM_RATE:.0%}"
                )
            if not stable:
                failures.append(f"recent batch {index} still changes the UVZ or promise boundary")
        if len(recent_source_sets) == 2 and recent_source_sets[0].intersection(recent_source_sets[1]):
            failures.append("the two recent batches must contain different source IDs")

    return {
        "pass": not failures,
        "qualified_sources": qualified_sources,
        "ingested_records": ingested_records,
        "minimum_qualified_sources": MIN_QUALIFIED_SOURCES,
        "coverage": dimension_counts,
        "coverage_source_ids": coverage_source_ids,
        "minimum_sources_per_dimension": MIN_SOURCES_PER_DIMENSION,
        "high_impact_claims_total": claims_total,
        "high_impact_claims_supported": claims_supported,
        "minimum_sources_per_high_impact_claim": MIN_SOURCES_PER_HIGH_IMPACT_CLAIM,
        "recent_batches": batch_results,
        "required_batch_size": SATURATION_BATCH_SIZE,
        "maximum_new_material_claim_rate": MAX_NEW_CLAIM_RATE,
        "saturation_report_artifact_id": row["artifact_id"] if row else None,
        "failures": failures,
    }


def research_readiness(project_dir: str | Path) -> dict[str, Any]:
    project = require_project(project_dir)
    with closing(connect(project)) as connection:
        return research_readiness_from_connection(connection)


def record_gate(
    project_dir: str | Path,
    gate: str,
    status: str,
    artifact_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    project = require_project(project_dir)
    if gate not in {"uvz", "outline", "final"}:
        raise ValueError("gate must be uvz, outline, or final")
    if status not in {"approved", "rejected", "auto-approved"}:
        raise ValueError("gate status must be approved, rejected, or auto-approved")
    decision_id = f"D-{gate}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(6)}"
    with closing(connect(project)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if gate == "uvz" and status in {"approved", "auto-approved"}:
            readiness = research_readiness_from_connection(connection)
            if not readiness["pass"]:
                raise ValueError(
                    "UVZ approval blocked by research readiness: " + "; ".join(readiness["failures"])
                )
        artifact = None
        if artifact_id:
            artifact = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if not artifact:
                raise ValueError("artifact_id does not exist")
        if status in {"approved", "auto-approved"}:
            if artifact is None:
                raise ValueError("approved gates require an artifact_id")
            expected_type = GATE_ARTIFACT_TYPES[gate]
            if artifact["artifact_type"] != expected_type:
                raise ValueError(f"{gate} gate requires artifact type {expected_type}")
            if artifact["status"] not in {"ready", "approved"}:
                raise ValueError("approved gates require a ready or approved artifact")
            if gate == "outline":
                prior = latest_gate(connection, "uvz")
                if not prior or prior["status"] not in {"approved", "auto-approved"}:
                    raise ValueError("outline approval requires an approved UVZ gate")
            if gate == "final":
                prior = latest_gate(connection, "outline")
                if not prior or prior["status"] not in {"approved", "auto-approved"}:
                    raise ValueError("final approval requires an approved outline gate")
                report_metadata = json.loads(artifact["metadata_json"] or "{}")
                if report_metadata.get("pass") is not True:
                    raise ValueError("final approval requires a passing quality report")
        connection.execute(
            "INSERT INTO decisions(decision_id, gate, status, artifact_id, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, gate, status, artifact_id, note, now_iso()),
        )
        log_event(connection, "gate_recorded", {"gate": gate, "status": status, "artifact_id": artifact_id})
        connection.commit()
    durability = sync_durable_state(project)
    return {
        "decision_id": decision_id,
        "gate": gate,
        "status": status,
        "artifact_id": artifact_id,
        "durability": durability,
    }


def latest_gate(connection: sqlite3.Connection, gate: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM decisions WHERE gate = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (gate,)
    ).fetchone()


def infer_stage(connection: sqlite3.Connection) -> str:
    source_count = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    ingested_count = connection.execute("SELECT COUNT(*) FROM sources WHERE status = 'ingested'").fetchone()[0]
    types = {row[0] for row in connection.execute("SELECT DISTINCT artifact_type FROM artifacts")}
    uvz_gate = latest_gate(connection, "uvz")
    outline_gate = latest_gate(connection, "outline")
    final_gate = latest_gate(connection, "final")
    if final_gate and final_gate["status"] in {"approved", "auto-approved"}:
        return "final-approved"
    if "quality-report" in types:
        return "qa-complete"
    if "ebook" in types:
        return "assembled"
    if "chapter" in types:
        return "drafting"
    if outline_gate and outline_gate["status"] in {"approved", "auto-approved"}:
        return "outline-approved"
    if "ebook-outline" in types or "ebook-charter" in types:
        return "charter-ready"
    if uvz_gate and uvz_gate["status"] in {"approved", "auto-approved"}:
        return "uvz-approved"
    if "uvz-analysis" in types:
        return "uvz-ready"
    if "evidence-map" in types:
        return "evidence-mapped"
    if ingested_count:
        return "sources-ingested"
    if source_count:
        return "sources-registered"
    if "search-plan" in types:
        return "search-planned"
    return "initialized"


def project_status(project_dir: str | Path) -> dict[str, Any]:
    project = require_project(project_dir)
    metadata = read_project_metadata(project)
    with closing(connect(project)) as connection:
        init_schema(connection)
        source_counts = {
            row["status"]: row["count"]
            for row in connection.execute("SELECT status, COUNT(*) AS count FROM sources GROUP BY status")
        }
        artifact_counts = {
            row["artifact_type"]: row["count"]
            for row in connection.execute(
                "SELECT artifact_type, COUNT(*) AS count FROM artifacts GROUP BY artifact_type"
            )
        }
        decisions = {
            gate: dict(row) if (row := latest_gate(connection, gate)) else None
            for gate in ("uvz", "outline", "final")
        }
        stage = infer_stage(connection)
        readiness = research_readiness_from_connection(connection)
    return {
        "project": str(project),
        "project_id": metadata["project_id"],
        "topic": metadata["topic"],
        "language": metadata.get("language"),
        "storage_policy": metadata.get("storage_policy", "legacy-local"),
        "mirror_root": metadata.get("mirror_root"),
        "import_roots": metadata.get("import_roots", [str(project / "input")]),
        "stage": stage,
        "source_counts": source_counts,
        "artifact_counts": artifact_counts,
        "gates": decisions,
        "research_readiness": readiness,
    }


def build_context_packet(
    project_dir: str | Path,
    stage: str,
    query: str | None = None,
    chapter_number: int | None = None,
    max_chars: int = 30000,
) -> dict[str, Any]:
    project = require_project(project_dir)
    if stage not in {"research", "uvz", "charter", "chapter", "qa"}:
        raise ValueError("stage must be research, uvz, charter, chapter, or qa")
    if max_chars < 2000:
        raise ValueError("max_chars must be at least 2000")
    metadata = read_project_metadata(project)
    sections = [
        "\n".join(
            [
                f"# Context packet: {stage}",
                f"Project: {metadata['project_id']}",
                f"Topic: {metadata['topic']}",
                f"Language: {metadata.get('language', '')}",
            ]
        )
    ]
    truncated = False

    def append_bounded(block: str) -> None:
        nonlocal truncated
        used = len("\n\n".join(sections))
        remaining = max_chars - used - 2
        if remaining <= 100:
            truncated = True
            return
        if len(block) > remaining:
            marker = "\n\n[Section truncated to preserve higher-priority evidence.]"
            sections.append(block[: max(0, remaining - len(marker))] + marker)
            truncated = True
        else:
            sections.append(block)

    artifact_sets = {
        "research": ["search-plan"],
        "uvz": ["search-plan", "evidence-map", "market-map", "source-note"],
        "charter": ["uvz-analysis", "evidence-map", "market-map"],
        "chapter": ["ebook-charter", "ebook-outline", "terminology-ledger", "claims-ledger", "continuity-summary"],
        "qa": ["ebook-charter", "ebook-outline", "terminology-ledger", "claims-ledger", "continuity-summary", "ebook"],
    }
    retrieval: dict[str, Any] | None = None
    retrieved_ids: list[str] = []
    if query:
        retrieval = search_corpus(project, query, limit=10)
        evidence = []
        for result in retrieval["results"]:
            evidence.append(
                f"### [{result['source_id']}] {result['title']} / {result['chunk_id']}\n{result['snippet']}"
            )
            if result["source_id"] not in retrieved_ids:
                retrieved_ids.append(result["source_id"])
        if evidence:
            evidence_block = "## Retrieved evidence\n\n" + "\n\n".join(evidence)
            evidence_budget = max(800, int(max_chars * 0.5))
            if len(evidence_block) > evidence_budget:
                evidence_block = (
                    evidence_block[: evidence_budget - 55]
                    + "\n\n[Evidence excerpts truncated; retrieval IDs are preserved.]"
                )
                truncated = True
            append_bounded(evidence_block)
    with closing(connect(project)) as connection:
        for artifact_type in artifact_sets[stage]:
            latest = latest_artifact(project, connection, artifact_type)
            if latest:
                row, content = latest
                append_bounded(f"## Artifact {artifact_type} {row['artifact_id']}\n{content}")
        if stage == "chapter" and chapter_number is not None:
            append_bounded(f"Requested chapter number: {chapter_number}")
        if retrieved_ids:
            placeholders = ",".join("?" for _ in retrieved_ids)
            source_rows = connection.execute(
                f"SELECT source_id, title, url, source_type, status FROM sources WHERE source_id IN ({placeholders}) ORDER BY source_id",
                retrieved_ids,
            ).fetchall()
        else:
            source_rows = connection.execute(
                "SELECT source_id, title, url, source_type, status FROM sources ORDER BY source_id LIMIT 50"
            ).fetchall()
        if source_rows:
            source_lines = [
                f"- [{row['source_id']}] {row['title']} | {row['source_type']} | {row['status']} | {row['url'] or 'local'}"
                for row in source_rows
            ]
            append_bounded("## Relevant source register\n" + "\n".join(source_lines))
    packet = "\n\n".join(sections)
    return {
        "stage": stage,
        "characters": len(packet),
        "truncated": truncated,
        "retrieval_id": retrieval.get("retrieval_id") if retrieval else None,
        "source_ids": retrieved_ids,
        "packet": packet,
    }


def chapter_artifacts(project: Path, connection: sqlite3.Connection) -> list[tuple[int, sqlite3.Row, str]]:
    chapters: list[tuple[int, sqlite3.Row, str]] = []
    rows = connection.execute(
        "SELECT * FROM artifacts WHERE artifact_type = 'chapter' AND status IN ('ready', 'approved') ORDER BY version DESC"
    ).fetchall()
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        number = metadata.get("chapter_number")
        if isinstance(number, int):
            path = project / validate_local_path(row["file_path"])
            chapters.append((number, row, path.read_text(encoding="utf-8")))
    latest_by_number: dict[int, tuple[sqlite3.Row, str]] = {}
    for number, row, content in chapters:
        current = latest_by_number.get(number)
        if not current or row["version"] > current[0]["version"]:
            latest_by_number[number] = (row, content)
    return [(number, row, content) for number, (row, content) in sorted(latest_by_number.items())]


def assemble_ebook(project_dir: str | Path, title: str | None = None) -> dict[str, Any]:
    project = require_project(project_dir)
    metadata = json.loads((project / "project.json").read_text(encoding="utf-8"))
    with closing(connect(project)) as connection:
        chapters = chapter_artifacts(project, connection)
        if not chapters:
            raise ValueError("no chapter artifacts with chapter_number metadata")
        resolved_title = title or metadata["topic"]
        body = f"# {resolved_title}\n\n" + "\n\n".join(content.strip() for _, _, content in chapters) + "\n"
        cited_ids = sorted(set(CITATION_RE.findall(body)))
        if cited_ids:
            placeholders = ",".join("?" for _ in cited_ids)
            source_rows = connection.execute(
                f"SELECT source_id, title, publisher, published_at, url, source_type FROM sources WHERE status = 'ingested' AND source_id IN ({placeholders}) ORDER BY source_id",
                cited_ids,
            ).fetchall()
        else:
            source_rows = []
    sources = ["# Sources", ""]
    for row in source_rows:
        details = ", ".join(value for value in (row["publisher"], row["published_at"], row["source_type"]) if value)
        target = row["url"] or "local source"
        sources.append(f"- [{row['source_id']}] {row['title']}. {details}. {target}")
    sources_text = "\n".join(sources).rstrip() + "\n"
    manuscript = body.rstrip() + "\n\n" + sources_text
    artifact = save_artifact(project, "ebook", manuscript, status="ready", metadata={"chapters": len(chapters)})
    output_path = project / "outputs" / "ebook.md"
    atomic_write_text(output_path, manuscript)
    sources_path = project / "outputs" / "sources.md"
    atomic_write_text(sources_path, sources_text)
    durability = sync_durable_state(project, [output_path.relative_to(project), sources_path.relative_to(project)])
    return {
        "artifact": artifact,
        "ebook_path": str(output_path),
        "sources_path": str(sources_path),
        "chapters": len(chapters),
        "cited_sources": len(cited_ids),
        "durability": durability,
    }


def word_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}|[\u4e00-\u9fff]{2,}", text.lower()))


def jaccard(left: str, right: str) -> float:
    a, b = word_set(left), word_set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def quality_check(project_dir: str | Path, duplicate_threshold: float = 0.82) -> dict[str, Any]:
    project = require_project(project_dir)
    manuscript_path = project / "outputs" / "ebook.md"
    if not manuscript_path.exists():
        raise ValueError("assemble the ebook before running quality_check")
    manuscript = manuscript_path.read_text(encoding="utf-8")
    with closing(connect(project)) as connection:
        source_ids = {row[0] for row in connection.execute("SELECT source_id FROM sources")}
        ingested_ids = {
            row[0] for row in connection.execute("SELECT source_id FROM sources WHERE status = 'ingested'")
        }
        chapters = chapter_artifacts(project, connection)
        gates = {gate: latest_gate(connection, gate) for gate in ("uvz", "outline", "final")}
        unreadable = [
            dict(row)
            for row in connection.execute(
                "SELECT source_id, title, status, extractor FROM sources WHERE status IN ('unreadable', 'fetch-failed')"
            )
        ]
        readiness = research_readiness_from_connection(connection)
    cited_ids = set(CITATION_RE.findall(manuscript))
    unknown_citations = sorted(cited_ids - source_ids)
    unprocessed_citations = sorted(cited_ids - ingested_ids)
    unresolved_patterns = {
        marker: len(re.findall(pattern, manuscript, flags=re.IGNORECASE))
        for marker, pattern in {
            "TODO": r"\bTODO\b",
            "VERIFY": r"\[VERIFY[^\]]*\]",
            "SOURCE": r"\[SOURCE\??[^\]]*\]",
            "template": r"\{\{[^}]+\}\}",
        }.items()
    }
    paragraphs: list[tuple[int, str]] = []
    chapter_citation_mismatches = []
    chapter_numbers = [chapter_number for chapter_number, _, _ in chapters]
    expected_numbers = list(range(1, max(chapter_numbers, default=0) + 1))
    for chapter_number, row, content in chapters:
        declared = set(json.loads(row["metadata_json"] or "{}").get("source_ids") or [])
        inline = set(CITATION_RE.findall(content))
        if declared != inline:
            chapter_citation_mismatches.append(
                {
                    "chapter": chapter_number,
                    "declared_only": sorted(declared - inline),
                    "inline_only": sorted(inline - declared),
                }
            )
        for paragraph in re.split(r"\n\s*\n", content):
            paragraph = paragraph.strip()
            if len(paragraph) >= 180:
                paragraphs.append((chapter_number, paragraph))
    duplicate_pairs = []
    for index, (left_chapter, left) in enumerate(paragraphs):
        for right_chapter, right in paragraphs[index + 1 :]:
            if left_chapter == right_chapter:
                continue
            score = jaccard(left, right)
            if score >= duplicate_threshold:
                duplicate_pairs.append(
                    {
                        "chapters": [left_chapter, right_chapter],
                        "similarity": round(score, 3),
                        "left": left[:180],
                        "right": right[:180],
                    }
                )
    approved = {
        gate: bool(row and row["status"] in {"approved", "auto-approved"})
        for gate, row in gates.items()
    }
    warnings = []
    if not approved["uvz"]:
        warnings.append("UVZ gate is not approved")
    if not approved["outline"]:
        warnings.append("Outline gate is not approved")
    if not readiness["pass"]:
        warnings.append("Research readiness and saturation standard is not satisfied")
    if chapter_numbers != expected_numbers:
        warnings.append("Chapter numbers must be contiguous and start at one")
    if not cited_ids:
        warnings.append("Manuscript contains no source citations")
    if unknown_citations:
        warnings.append("Manuscript contains unknown source IDs")
    if unprocessed_citations:
        warnings.append("Manuscript cites sources that were not ingested")
    if any(unresolved_patterns.values()):
        warnings.append("Manuscript contains unresolved drafting markers")
    if duplicate_pairs:
        warnings.append("Potential cross-chapter duplicate paragraphs found")
    if chapter_citation_mismatches:
        warnings.append("Chapter source_ids metadata does not match inline citations")
    report = {
        "checked_at": now_iso(),
        "pass": not warnings,
        "warnings": warnings,
        "chapters": len(chapters),
        "registered_sources": len(source_ids),
        "ingested_sources": len(ingested_ids),
        "cited_sources": len(cited_ids),
        "chapter_numbers": chapter_numbers,
        "expected_chapter_numbers": expected_numbers,
        "chapter_citation_mismatches": chapter_citation_mismatches,
        "unknown_citations": unknown_citations,
        "unprocessed_citations": unprocessed_citations,
        "unreadable_sources": unreadable,
        "unresolved_markers": unresolved_patterns,
        "duplicate_pairs": duplicate_pairs,
        "gates": approved,
        "research_readiness": readiness,
    }
    lines = ["# Quality report", "", f"Status: {'PASS' if report['pass'] else 'NEEDS REVIEW'}", ""]
    if warnings:
        lines.extend(["## Warnings", ""] + [f"- {item}" for item in warnings] + [""])
    lines.extend(
        [
            "## Counts",
            "",
            f"- Chapters: {report['chapters']}",
            f"- Registered sources: {report['registered_sources']}",
            f"- Ingested sources: {report['ingested_sources']}",
            f"- Cited sources: {report['cited_sources']}",
            f"- Potential duplicate pairs: {len(duplicate_pairs)}",
            "",
            "## Machine-readable detail",
            "",
            "```json",
            json.dumps(report, ensure_ascii=False, indent=2),
            "```",
        ]
    )
    report_text = "\n".join(lines) + "\n"
    output_path = project / "outputs" / "quality-report.md"
    atomic_write_text(output_path, report_text)
    artifact = save_artifact(
        project,
        "quality-report",
        report_text,
        status="ready",
        metadata={"pass": report["pass"], "ebook_sha256": sha256_text(manuscript)},
    )
    sync_durable_state(project, [output_path.relative_to(project)])
    report["path"] = str(output_path)
    report["artifact"] = artifact
    return report


def verify_knowledge_base(project_dir: str | Path) -> dict[str, Any]:
    project = require_project(project_dir)
    metadata = read_project_metadata(project)
    errors: list[str] = []
    warnings: list[str] = []
    marker_path = project / PROJECT_MARKER
    if not marker_path.is_file():
        errors.append("project safety marker is missing")
    else:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("project_uuid") != metadata.get("project_uuid"):
            errors.append("project safety marker does not match project metadata")

    checked_files: list[str] = []
    with closing(connect(project)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append(f"database integrity check failed: {integrity}")
        chunk_total = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        try:
            fts_total = connection.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
            if fts_total != chunk_total:
                errors.append(f"FTS index contains {fts_total} rows for {chunk_total} chunks")
        except sqlite3.OperationalError:
            warnings.append("SQLite FTS5 is unavailable; retrieval is using the lexical fallback")
        sources = connection.execute(
            "SELECT source_id, status, sha256, text_sha256, raw_path, text_path FROM sources"
        ).fetchall()
        artifacts = connection.execute("SELECT artifact_id, file_path, sha256 FROM artifacts").fetchall()
        for row in sources:
            if not row["raw_path"] and not row["text_path"]:
                continue
            raw_path = project / validate_local_path(row["raw_path"] or "")
            text_path = project / validate_local_path(row["text_path"] or "")
            if not row["raw_path"] or not raw_path.is_file():
                errors.append(f"{row['source_id']} raw source is missing")
            elif sha256_file(raw_path) != row["sha256"]:
                errors.append(f"{row['source_id']} raw source hash mismatch")
            else:
                checked_files.append(row["raw_path"])
            if not row["text_path"] and row["status"] not in {"ingested", "awaiting-review"}:
                continue
            if not row["text_path"] or not text_path.is_file():
                errors.append(f"{row['source_id']} normalized text is missing")
            elif row["text_sha256"] and sha256_file(text_path) != row["text_sha256"]:
                errors.append(f"{row['source_id']} normalized text hash mismatch")
            else:
                checked_files.append(row["text_path"])
            chunk_count = connection.execute(
                "SELECT COUNT(*) FROM chunks WHERE source_id = ?", (row["source_id"],)
            ).fetchone()[0]
            if chunk_count < 1:
                errors.append(f"{row['source_id']} has no searchable chunks")
        for row in artifacts:
            path = project / validate_local_path(row["file_path"])
            if not path.is_file():
                errors.append(f"{row['artifact_id']} artifact file is missing")
            elif (
                sha256_file(path) != row["sha256"]
                and sha256_text(path.read_text(encoding="utf-8").rstrip("\n")) != row["sha256"]
            ):
                errors.append(f"{row['artifact_id']} artifact hash mismatch")
            else:
                checked_files.append(row["file_path"])

    mirror = mirror_project_path(project, metadata.get("mirror_root"), metadata["project_id"])
    mirror_verified = False
    if mirror is None:
        warnings.append("project has no independent mirror; a host or volume failure can destroy the knowledge base")
    elif not mirror.is_dir():
        errors.append("configured project mirror is missing")
    else:
        mirror_marker = mirror / PROJECT_MARKER
        if not mirror_marker.is_file():
            errors.append("mirror safety marker is missing")
        else:
            mirror_marker_data = json.loads(mirror_marker.read_text(encoding="utf-8"))
            if mirror_marker_data.get("project_uuid") != metadata.get("project_uuid"):
                errors.append("mirror safety marker does not match project metadata")
        for relative in dict.fromkeys(checked_files):
            primary_file = project / relative
            mirror_file = mirror / relative
            if not mirror_file.is_file():
                errors.append(f"mirror is missing {relative}")
            elif sha256_file(primary_file) != sha256_file(mirror_file):
                errors.append(f"mirror hash mismatch for {relative}")
        mirror_database = mirror / "state.sqlite3"
        if not mirror_database.is_file():
            errors.append("mirror database snapshot is missing")
        else:
            with closing(sqlite3.connect(mirror_database)) as connection:
                mirror_integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if mirror_integrity != "ok":
                errors.append(f"mirror database integrity check failed: {mirror_integrity}")
        mirror_verified = not errors

    return {
        "pass": not errors,
        "project": str(project),
        "project_uuid": metadata.get("project_uuid"),
        "storage_policy": metadata.get("storage_policy"),
        "mirror": str(mirror) if mirror else None,
        "mirror_verified": mirror_verified,
        "checked_files": len(set(checked_files)),
        "errors": errors,
        "warnings": warnings,
    }


def restore_knowledge_base(mirror_project_dir: str | Path, primary_root: str | Path) -> dict[str, Any]:
    validate_local_path(mirror_project_dir)
    validate_local_path(primary_root)
    restored = restore_project_from_mirror(
        Path(mirror_project_dir), Path(primary_root)
    )
    metadata = read_project_metadata(restored)
    metadata["import_roots"] = [str(restored / "input")]
    atomic_write_text(
        restored / "project.json",
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
    )
    sync_durable_state(restored)
    verification = verify_knowledge_base(restored)
    if not verification["pass"]:
        raise OSError("restored knowledge base failed verification: " + "; ".join(verification["errors"]))
    return {"status": "restored", "project": str(restored), "verification": verification}


def prepare_ebook_retention(
    project_dir: str | Path,
    release_dir: str | Path,
    ttl_minutes: int = FINALIZATION_TTL_MINUTES,
) -> dict[str, Any]:
    validate_local_path(project_dir)
    release = local_path(release_dir)
    home = local_path("~")
    project = require_project(project_dir)
    if ttl_minutes < 5 or ttl_minutes > 1440:
        raise ValueError("ttl_minutes must be between 5 and 1440")
    verification = verify_knowledge_base(project)
    if not verification["pass"]:
        raise ValueError("knowledge base integrity check failed: " + "; ".join(verification["errors"]))
    ebook_path = project / "outputs" / "ebook.md"
    if not ebook_path.is_file():
        raise ValueError("ebook.md does not exist")
    ebook_digest = sha256_file(ebook_path)
    with closing(connect(project)) as connection:
        quality = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_type = 'quality-report' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        final_gate = latest_gate(connection, "final")
        if not quality or json.loads(quality["metadata_json"] or "{}").get("pass") is not True:
            raise ValueError("latest quality report must pass before preparing retention")
        quality_metadata = json.loads(quality["metadata_json"] or "{}")
        if quality_metadata.get("ebook_sha256") != sha256_text(ebook_path.read_text(encoding="utf-8")):
            raise ValueError("ebook changed after the latest quality check")
        if (
            not final_gate
            or final_gate["status"] not in {"approved", "auto-approved"}
            or final_gate["artifact_id"] != quality["artifact_id"]
        ):
            raise ValueError("final gate must approve the latest passing quality report")

    release = release.resolve()
    if is_relative_to(release, project) or release == project:
        raise ValueError("release_dir must be outside the project that will be deleted")
    if release == Path(release.anchor) or release == home.resolve() or len(release.parts) < 3:
        raise ValueError("release_dir is too broad")
    release.mkdir(parents=True, exist_ok=True)
    existing = list(release.iterdir())
    if existing:
        raise ValueError("release_dir must be empty so the retained output is exactly one ebook")
    retained_ebook = release / "ebook.md"
    atomic_copy(ebook_path, retained_ebook)
    if sha256_file(retained_ebook) != ebook_digest:
        retained_ebook.unlink(missing_ok=True)
        raise OSError("retained ebook hash verification failed")

    token = secrets.token_urlsafe(32)
    finalization_id = f"F-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(5)}"
    prepared_at = datetime.now(timezone.utc)
    expires_at = prepared_at + timedelta(minutes=ttl_minutes)
    with closing(connect(project)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE finalizations SET status = 'superseded' WHERE status = 'prepared'")
        connection.execute(
            """
            INSERT INTO finalizations(
                finalization_id, release_path, ebook_sha256, token_sha256,
                prepared_at, expires_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'prepared')
            """,
            (
                finalization_id,
                str(retained_ebook),
                ebook_digest,
                sha256_text(token),
                prepared_at.isoformat(timespec="seconds"),
                expires_at.isoformat(timespec="seconds"),
            ),
        )
        log_event(
            connection,
            "ebook_retention_prepared",
            {"finalization_id": finalization_id, "release_path": str(retained_ebook)},
        )
        connection.commit()
    durability = sync_durable_state(project)
    confirmation_phrase = f"PURGE {read_project_metadata(project)['project_id']} KEEP {ebook_digest[:12]}"
    return {
        "status": "prepared",
        "finalization_id": finalization_id,
        "retained_ebook": str(retained_ebook),
        "ebook_sha256": ebook_digest,
        "purge_token": token,
        "confirmation_phrase": confirmation_phrase,
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "knowledge_base_still_present": True,
        "next_action": "Ask the user to send the confirmation phrase exactly, then call purge_knowledge_base with the phrase and token.",
        "durability": durability,
    }


def purge_knowledge_base(
    project_dir: str | Path,
    purge_token: str,
    confirmation_phrase: str,
) -> dict[str, Any]:
    project = require_project(project_dir)
    metadata = read_project_metadata(project)
    verification = verify_knowledge_base(project)
    if not verification["pass"]:
        raise ValueError("knowledge base integrity check failed before purge")
    with closing(connect(project)) as connection:
        finalization = connection.execute(
            "SELECT * FROM finalizations WHERE token_sha256 = ? AND status IN ('prepared', 'purge-started') ORDER BY prepared_at DESC LIMIT 1",
            (sha256_text(purge_token),),
        ).fetchone()
        if not finalization:
            raise ValueError("purge token is invalid or no longer active")
        expires_at = datetime.fromisoformat(finalization["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            raise ValueError("purge token has expired; prepare retention again")
        retained_ebook = local_path(finalization["release_path"]).resolve()
        if not retained_ebook.is_file():
            raise ValueError("retained ebook is missing")
        if sha256_file(retained_ebook) != finalization["ebook_sha256"]:
            raise ValueError("retained ebook hash does not match the prepared release")
        expected_confirmation = (
            f"PURGE {metadata['project_id']} KEEP {finalization['ebook_sha256'][:12]}"
        )
        if confirmation_phrase != expected_confirmation:
            raise ValueError("confirmation phrase does not match the prepared release")
        if is_relative_to(retained_ebook, project):
            raise ValueError("retained ebook is inside the project and would be deleted")
        siblings = list(retained_ebook.parent.iterdir())
        if siblings != [retained_ebook]:
            raise ValueError("release directory no longer contains exactly one ebook")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE finalizations SET status = 'purge-started' WHERE finalization_id = ?",
            (finalization["finalization_id"],),
        )
        connection.commit()

    sync_durable_state(project)
    mirror = mirror_project_path(project, metadata.get("mirror_root"), metadata["project_id"])
    if mirror and mirror.exists():
        guarded_remove_project(mirror, metadata["project_uuid"])
    if not retained_ebook.is_file() or sha256_file(retained_ebook) != finalization["ebook_sha256"]:
        raise OSError("retained ebook failed verification after mirror deletion; primary was preserved")
    guarded_remove_project(project, metadata["project_uuid"])
    if not retained_ebook.is_file() or sha256_file(retained_ebook) != finalization["ebook_sha256"]:
        raise OSError("retained ebook failed post-purge verification")
    if list(retained_ebook.parent.iterdir()) != [retained_ebook]:
        raise OSError("release directory contains unexpected files after purge")
    return {
        "status": "purged",
        "retained_ebook": str(retained_ebook),
        "ebook_sha256": finalization["ebook_sha256"],
        "primary_deleted": not project.exists(),
        "mirror_deleted": mirror is None or not mirror.exists(),
        "retained_files": [str(retained_ebook)],
    }


def cli() -> int:
    parser = argparse.ArgumentParser(description="Topic to Ebook engine")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("root")
    init.add_argument("slug")
    init.add_argument("topic")
    init.add_argument("--language", default="zh-CN")
    init.add_argument("--audience-hint")
    init.add_argument("--mirror-root")
    init.add_argument("--import-root", action="append")
    init.add_argument("--allow-single-copy", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("project")
    search = sub.add_parser("search")
    search.add_argument("project")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)
    ingest = sub.add_parser("ingest-file")
    ingest.add_argument("project")
    ingest.add_argument("file")
    ingest.add_argument("--title")
    assemble = sub.add_parser("assemble")
    assemble.add_argument("project")
    assemble.add_argument("--title")
    qa = sub.add_parser("qa")
    qa.add_argument("project")
    verify = sub.add_parser("verify")
    verify.add_argument("project")
    args = parser.parse_args()
    if args.command == "init":
        result = init_project(
            args.root,
            args.slug,
            args.topic,
            args.language,
            args.audience_hint,
            args.mirror_root,
            args.import_root,
            args.allow_single_copy,
        )
    elif args.command == "status":
        result = project_status(args.project)
    elif args.command == "search":
        result = search_corpus(args.project, args.query, args.limit)
    elif args.command == "ingest-file":
        result = ingest_file(args.project, args.file, args.title)
    elif args.command == "assemble":
        result = assemble_ebook(args.project, args.title)
    elif args.command == "qa":
        result = quality_check(args.project)
    elif args.command == "verify":
        result = verify_knowledge_base(args.project)
    else:
        raise AssertionError(args.command)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(cli())
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise
