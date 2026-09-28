#!/usr/bin/env python3
"""Crash-resistant file storage helpers for Topic to Ebook projects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import secrets
import shutil
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path, PureWindowsPath
from typing import Iterable
from ebe.isolation import validate_local_path


PROJECT_MARKER = ".topic-to-ebook-project.json"
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
BUSINESS_DIRS = {"input", "sources", "artifacts", "outputs", "logs"}
BUSINESS_SUFFIXES = {".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".xml",
                     ".html", ".htm", ".pdf", ".docx", ".bin", ".log"}


def local_path(value: str | Path) -> Path:
    """Validate before expansion and again before any caller may resolve/stat."""
    expanded = Path(validate_local_path(value)).expanduser()
    validate_local_path(expanded)
    return expanded


def reject_links(path: Path) -> None:
    """Check lexically, before resolve() can hide a symlink or Windows junction."""
    validate_local_path(path)
    path = Path(validate_local_path(os.path.abspath(path)))
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("symlink_or_reparse_path")


def relative_business_path(value: str | Path) -> Path:
    validate_local_path(value)
    raw = str(value)
    windows = PureWindowsPath(raw)
    path = Path(raw)
    if (path.is_absolute() or windows.drive or windows.root or
            ".." in windows.parts or ".." in path.parts or ":" in raw):
        raise ValueError("invalid_mirror_relative_path")
    if not path.parts or (path.as_posix() not in {"project.json", PROJECT_MARKER, "state.sqlite3"} and
            (path.parts[0] not in BUSINESS_DIRS or
             (path.suffix.lower() not in BUSINESS_SUFFIXES and not
              (path.parts[:2] == ("sources", "raw") and
               re.fullmatch(r"S\d{4,}-[a-f0-9]{12}\.[A-Za-z0-9_-]+", path.name))) or
             any(p.startswith(".") for p in path.parts))):
        raise ValueError("non_business_mirror_path")
    return path


def checked_tree(root: Path):
    reject_links(root)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            reject_links(path)
            if name in files:
                yield path

# Scoped to the caller, never shared with download threads or other projects.
_sync_batch = ContextVar("mirror_sync_batch", default=None)


@contextmanager
def batch_durable_sync(project: Path):
    reject_links(project)
    project = project.resolve()
    active = _sync_batch.get()
    if active is not None and active[0] == project:
        yield
        return
    paths: set[Path] = set()
    database = project / "state.sqlite3"
    def committed_files(database_path=database):
        reject_links(database_path)
        for suffix in ("-wal", "-shm", "-journal"):
            reject_links(Path(str(database_path) + suffix))
        if not database_path.is_file():
            return {}
        with closing(sqlite3.connect(database_path)) as conn:
            return {row[0]: row[1:] for row in conn.execute(
                "SELECT source_id,raw_path,text_path,sha256,text_sha256 FROM sources")}
    before = committed_files()
    reject_links(project / "project.json")
    metadata = json.loads((project / "project.json").read_text(encoding="utf-8"))
    mirror = mirror_project_path(project, metadata.get("mirror_root"), metadata["project_id"])
    if mirror is not None:
        # Use the last published snapshot, so a failed prior mirror copy is retried.
        before = committed_files(mirror / "state.sqlite3")
    token = _sync_batch.set((project, paths))
    try:
        yield
    finally:
        _sync_batch.reset(token)
        # Also collect committed paths if interruption occurred between COMMIT
        # and a caller's sync request. Copy files before publishing the DB backup.
        for source_id, row in committed_files().items():
            if before.get(source_id) != row:
                paths.update(Path(p) for p in row[:2] if p)
        sync_project_mirror(project, paths)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    validate_local_path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync; Windows does not expose portable directory fsync."""
    validate_local_path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    reject_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".part", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_copy(source: Path, destination: Path) -> None:
    validate_local_path(source)
    validate_local_path(destination)
    reject_links(source)
    reject_links(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        try:
            shutil.copystat(source, temporary)
        except OSError:
            pass
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def mirror_project_path(project: Path, mirror_root: str | Path | None, project_id: str) -> Path | None:
    validate_local_path(project)
    mirror_base = local_path(mirror_root) if mirror_root else None
    if not isinstance(project_id, str) or not PROJECT_SLUG_RE.fullmatch(project_id):
        raise ValueError("invalid_project_slug")
    reject_links(project)
    if not mirror_root:
        return None
    reject_links(mirror_base)
    reject_links(mirror_base / project_id)
    mirror = mirror_base.resolve() / project_id
    project = project.resolve()
    if mirror == project or is_relative_to(mirror, project) or is_relative_to(project, mirror):
        raise ValueError("mirror_root must be on a separate path outside the project tree")
    return mirror


def backup_sqlite(source: Path, destination: Path) -> None:
    validate_local_path(source)
    validate_local_path(destination)
    reject_links(source)
    reject_links(destination)
    for database in (source, destination):
        for suffix in ("-wal", "-shm", "-journal"):
            reject_links(Path(str(database) + suffix))
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_connection = sqlite3.connect(source, timeout=30)
        target_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_project_mirror(project: Path, relative_paths: Iterable[str | Path] = ()) -> dict[str, object]:
    validate_local_path(project)
    relative_paths = [relative_business_path(p) for p in relative_paths]
    reject_links(project)
    reject_links(project / "project.json")
    for relative in relative_paths:
        reject_links(project / relative)
    active = _sync_batch.get()
    if active is not None and active[0] == project.resolve():
        active[1].update(Path(p) for p in relative_paths)
        return {"mode": "deferred", "verified": False}
    source_files = list(checked_tree(project))
    metadata = json.loads((project / "project.json").read_text(encoding="utf-8"))
    mirror = mirror_project_path(project, metadata.get("mirror_root"), metadata["project_id"])
    if mirror is None:
        return {"mode": "single-copy", "mirror": None, "verified": False}
    new_mirror = not mirror.exists()
    if not new_mirror:
        # Inspect even unused entries so a planted junction cannot be traversed later.
        for _ in checked_tree(mirror):
            pass
    mirror.mkdir(parents=True, exist_ok=True)
    required = [Path("project.json"), Path(PROJECT_MARKER)]
    required.extend(Path(item) for item in relative_paths)
    if new_mirror:
        for item in source_files:
            relative = item.relative_to(project)
            if relative.name.startswith("state.sqlite3"):
                continue
            try:
                required.append(relative_business_path(relative))
            except ValueError:
                continue
    copied: list[str] = []
    for relative in dict.fromkeys(required):
        if relative == Path("state.sqlite3"):
            continue  # SQLite must go through backup(), never a live file copy.
        source = project / relative
        reject_links(source)
        if not source.is_file():
            continue
        destination = mirror / relative
        atomic_copy(source, destination)
        if sha256_file(source) != sha256_file(destination):
            raise OSError(f"mirror verification failed for {relative.as_posix()}")
        copied.append(relative.as_posix())
    database = project / "state.sqlite3"
    if database.is_file():
        backup_sqlite(database, mirror / "state.sqlite3")
        with closing(sqlite3.connect(mirror / "state.sqlite3")) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise OSError(f"mirror database integrity check failed: {integrity}")
    return {"mode": "mirrored", "mirror": str(mirror), "verified": True, "copied": copied}


def guarded_remove_project(project: Path, project_uuid: str) -> None:
    reject_links(project)
    home = local_path("~")
    project = project.resolve()
    marker = project / PROJECT_MARKER
    if not marker.is_file():
        raise ValueError(f"refusing to delete unmarked directory: {project}")
    marker_data = json.loads(marker.read_text(encoding="utf-8"))
    if marker_data.get("project_uuid") != project_uuid:
        raise ValueError(f"project marker mismatch: {project}")
    if project == Path(project.anchor) or project == home.resolve() or len(project.parts) < 4:
        raise ValueError(f"refusing to delete broad path: {project}")
    shutil.rmtree(project)
    if project.exists():
        raise OSError(f"project deletion did not complete: {project}")


def restore_project_from_mirror(mirror_project: Path, primary_root: Path) -> Path:
    validate_local_path(mirror_project)
    validate_local_path(primary_root)
    mirror_project = local_path(mirror_project)
    primary_root = local_path(primary_root)
    reject_links(mirror_project)
    reject_links(primary_root)
    mirror_project = mirror_project.resolve()
    primary_root = primary_root.resolve()
    marker_path = mirror_project / PROJECT_MARKER
    metadata_path = mirror_project / "project.json"
    database_path = mirror_project / "state.sqlite3"
    files = list(checked_tree(mirror_project))
    if not marker_path.is_file() or not metadata_path.is_file() or not database_path.is_file():
        raise ValueError("mirror is missing its project marker, metadata, or database")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("mirror_root"):
        local_path(metadata["mirror_root"])
    for value in metadata.get("import_roots") or []:
        local_path(value)
    if marker.get("project_uuid") != metadata.get("project_uuid"):
        raise ValueError("mirror marker does not match mirror metadata")
    project_id = metadata.get("project_id")
    if not isinstance(project_id, str) or not PROJECT_SLUG_RE.fullmatch(project_id):
        raise ValueError("mirror project_id is invalid")
    destination = primary_root / project_id
    reject_links(destination)
    if destination.exists():
        raise ValueError("restore destination already exists")
    if is_relative_to(destination, mirror_project) or is_relative_to(mirror_project, destination):
        raise ValueError("restore destination must be outside the mirror tree")
    primary_root.mkdir(parents=True, exist_ok=True)
    staging = primary_root / f".{project_id}.restore-{secrets.token_hex(6)}"
    staging.mkdir()
    try:
        for source in files:
            relative = source.relative_to(mirror_project)
            try:
                relative_business_path(relative)
            except ValueError:
                continue
            atomic_copy(source, staging / relative)
        with closing(sqlite3.connect(staging / "state.sqlite3")) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise OSError(f"restored database integrity check failed: {integrity}")
        os.replace(staging, destination)
        _fsync_directory(primary_root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination
