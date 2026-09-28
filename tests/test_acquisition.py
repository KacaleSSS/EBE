"""Offline acquisition contract tests; timings measure synthetic latency only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import Counter
from contextlib import closing, ExitStack
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ebe" / "legacy"))
import engine
import research
import storage


class AcquisitionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.make_project("acquisition")
        # Works before the separately owned network module is delivered.
        self.network = types.ModuleType("ebe.network")
        self.network.fetch = mock.Mock(side_effect=self.body)
        self.patch = mock.patch.dict(sys.modules, {"ebe.network": self.network})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def make_project(self, name):
        return Path(engine.init_project(self.root / "primary", name, "Synthetic test",
                    mirror_root=self.root / "mirror")["project"])

    @staticmethod
    def body(url, max_bytes, timeout):
        return ((url + " synthetic evidence explaining limitations and methods. " * 40).encode(),
                "text/plain", url)

    def candidates(self, count=8, same_domain=False):
        query = research.plan_research(str(self.project), queries=[{
            "query": "synthetic evidence", "dimension": "audience", "provider": "codex"}])["query_ids"][0]
        research.submit_search_results(str(self.project), query, 0, [
            {"url": f"https://d{0 if same_domain else i % 4}.example/{i}"} for i in range(count)])

    def run_batch(self, **kwargs):
        return research.run_research_batch(str(self.project), max_queries=0, **kwargs)

    def test_parallel_limits_single_writer_sync_and_resume(self):
        self.candidates(12, same_domain=True)
        owner = threading.get_ident()
        lock = threading.Lock()
        active = Counter()
        peaks = Counter()
        original_connect = engine.connect

        def connect(project):
            self.assertEqual(threading.get_ident(), owner, "SQLite used in downloader")
            return original_connect(project)

        def fetch(url, max_bytes, timeout):
            self.assertNotEqual(threading.get_ident(), owner)
            host = urlsplit(url).hostname
            with lock:
                active[host] += 1
                peaks[host] = max(peaks[host], active[host])
            try:
                time.sleep(.025)
                return self.body(url, max_bytes, timeout)
            finally:
                with lock:
                    active[host] -= 1

        self.network.fetch.side_effect = fetch
        with mock.patch.object(engine, "connect", side_effect=connect), mock.patch.object(
            engine, "ingest_url", side_effect=AssertionError("worker ingestion")), mock.patch.object(
            storage, "backup_sqlite", wraps=storage.backup_sqlite) as backup, mock.patch.object(
            engine, "verify_knowledge_base", wraps=engine.verify_knowledge_base) as verify:
            result = self.run_batch(workers=8)
            self.assertEqual(len(result["acquired_source_ids"]), 12)
            self.assertEqual(backup.call_count, 1)
            self.assertEqual(verify.call_count, 1)
        self.assertEqual(peaks["d0.example"], 2)
        self.assertEqual(engine.MIN_QUALIFIED_SOURCES, 120)
        self.assertEqual(engine.research_readiness(self.project)["qualified_sources"], 0)
        self.network.fetch.reset_mock()
        self.run_batch()
        self.network.fetch.assert_not_called()

    def test_single_thread_legacy_ingest_interface_and_redaction(self):
        self.candidates(1)
        self.network.fetch.side_effect = TimeoutError("https://secret.example/?token=SECRET")
        with mock.patch.object(engine, "ingest_url", wraps=engine.ingest_url) as ingest:
            result = self.run_batch(workers=1)
        ingest.assert_called_once()
        self.assertEqual(result["results"][0]["error"], "TimeoutError")
        with closing(research.connect(self.project)) as conn:
            self.assertEqual(conn.execute("SELECT last_error FROM acquisitions").fetchone()[0], "TimeoutError")
            dump = "\n".join(conn.iterdump())
        self.assertNotIn("SECRET", dump)
        self.network.fetch.side_effect = self.body
        research.retry_research(str(self.project))
        self.assertEqual(self.run_batch(workers=1)["results"][0]["status"], "awaiting-review")

    def test_retry_backoff_and_mirror_restore(self):
        self.candidates(2)
        self.network.fetch.side_effect = TimeoutError("SECRET")
        first = self.run_batch()
        self.assertTrue(all(r["error"] == "TimeoutError" for r in first["results"]))
        self.network.fetch.reset_mock()
        self.assertEqual(self.run_batch()["results"], [])
        self.network.fetch.assert_not_called()
        research.retry_research(str(self.project))
        self.network.fetch.side_effect = self.body
        self.run_batch()
        restored = engine.restore_knowledge_base(self.root / "mirror" / "acquisition", self.root / "restored")
        self.project = Path(restored["project"])
        self.network.fetch.reset_mock()
        self.run_batch()
        self.network.fetch.assert_not_called()
        self.assertTrue(engine.verify_knowledge_base(self.project)["pass"])

    def test_failed_mirror_copy_retries_without_download(self):
        self.candidates(1)
        copy = storage.atomic_copy

        def fail_source(source, target):
            if "sources" in source.parts:
                raise OSError("synthetic disk failure")
            return copy(source, target)

        with mock.patch.object(storage, "atomic_copy", side_effect=fail_source):
            with self.assertRaises(OSError):
                self.run_batch()
        self.network.fetch.reset_mock()
        self.run_batch()
        self.network.fetch.assert_not_called()
        self.assertTrue(engine.verify_knowledge_base(self.project)["pass"])

    def test_real_network_interface_is_used(self):
        self.patch.stop()
        from ebe import network
        response = self.body("https://example.org/a", 1024, 7)
        with mock.patch.object(network, "fetch", return_value=response) as fetch:
            self.assertEqual(engine.download_url("https://example.org/a", 10000, 7), response)
            fetch.assert_called_once_with("https://example.org/a", max_bytes=10000, timeout=7)

    def test_finally_sync_after_commit_before_sync_request(self):
        self.candidates(2)
        original = engine.commit_download

        def interrupt(*args, **kwargs):
            # Interrupt just after SQLite COMMIT, before the normal sync request.
            with mock.patch.object(engine, "sync_durable_state", side_effect=KeyboardInterrupt):
                return original(*args, **kwargs)

        with mock.patch.object(engine, "commit_download", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_batch()
        self.assertTrue(engine.verify_knowledge_base(self.project)["pass"])
        restored = engine.restore_knowledge_base(self.root / "mirror" / "acquisition", self.root / "restored")
        self.project = Path(restored["project"])
        self.network.fetch.reset_mock()
        self.run_batch()
        self.assertEqual(self.network.fetch.call_count, 1)
        self.assertTrue(self.network.fetch.call_args.args[0].endswith("/1"))

    def test_bounds_and_synthetic_benchmark(self):
        for workers in (0, 9, True, 1.5):
            with self.assertRaises(ValueError):
                self.run_batch(workers=workers)
        timings = {}
        peaks = {}
        for workers in (1, 4):
            self.project = self.make_project(f"bench-{workers}")
            self.candidates(8)
            lock = threading.Lock()
            active = 0
            peak = 0

            def fetch(url, max_bytes, timeout):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(.1)
                    return self.body(url, max_bytes, timeout)
                finally:
                    with lock:
                        active -= 1

            self.network.fetch.side_effect = fetch
            started = time.perf_counter()
            self.assertEqual(len(self.run_batch(workers=workers)["acquired_source_ids"]), 8)
            timings[workers] = time.perf_counter() - started
            peaks[workers] = peak
        self.assertEqual(peaks, {1: 1, 4: 4})
        # Timing is reported, not asserted: disk/CI scheduling can dominate.
        print("SYNTHETIC ONLY " + json.dumps({"seconds": timings,
              "speedup": timings[1] / timings[4], "injected_latency_seconds": .1}))

    def test_url_credentials_and_signed_queries_never_registered(self):
        for url in ("https://user:pw@example.org/", "https://@example.org/",
                    "https://example.org/?token=SECRET", "https://example.org/?%6bey=SECRET",
                    "https://example.org/?X-Amz-Date=SECRET", "https://example.org/?signature=SECRET",
                    "https://example.org/?api_key=SECRET", "https://example.org/?sig=SECRET"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    engine.register_sources(self.project, [{"url": url}])
                with self.assertRaises(ValueError):
                    research.clean_url(url)
        with closing(engine.connect(self.project)) as conn:
            self.assertNotIn("SECRET", "\n".join(conn.iterdump()))
        self.assertEqual(engine.canonical_url("https://example.org/?page=2#part"),
                         "https://example.org/?page=2")

    def test_mirror_paths_whitelist_and_restore_slug(self):
        for relative in ("../outside.txt", "/outside.txt", "C:\\outside.txt", "input/../outside.txt",
                         "input/../../outside.txt", "input/.env", "input/a.txt:secret", "\\\\host\\share"):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                storage.sync_project_mirror(self.project, [relative])
        (self.project / ".env").write_text("SECRET")
        (self.project / "input" / ".env").write_text("SECRET")
        metadata_path = self.project / "project.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["mirror_root"] = str(self.root / "fresh-mirror")
        metadata_path.write_text(json.dumps(metadata))
        storage.sync_project_mirror(self.project)
        mirror = self.root / "fresh-mirror" / "acquisition"
        self.assertFalse((mirror / ".env").exists())
        self.assertFalse((mirror / "input" / ".env").exists())
        for slug in ("../escape", "C:\\escape", "/escape", "has space"):
            metadata["project_id"] = slug
            (mirror / "project.json").write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, "project_id"):
                storage.restore_project_from_mirror(mirror, self.root / "restore")

    def test_symlink_and_reparse_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.project / "input" / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("symlinks unavailable")
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                    capture_output=True, check=False)
            if result.returncode:
                self.skipTest("symlinks and junctions unavailable")
        try:
            with self.assertRaisesRegex(ValueError, "reparse"):
                storage.sync_project_mirror(self.project)
        finally:
            if link.is_symlink():
                link.unlink()
            else:
                link.rmdir()
        mirror = self.root / "mirror" / "acquisition"
        target = mirror / "sources" / "raw"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.rmdir()
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(outside)], check=True,
                           stdout=subprocess.DEVNULL)
        else:
            target.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaisesRegex(ValueError, "reparse"):
                storage.sync_project_mirror(self.project)
            with self.assertRaisesRegex(ValueError, "reparse"):
                storage.restore_project_from_mirror(mirror, self.root / "restored")
        finally:
            if target.is_symlink():
                target.unlink()
            else:
                target.rmdir()

    def test_pdf_bounded_output_minimal_environment_and_timeout(self):
        pdf = self.project / "input" / "test.pdf"
        pdf.write_bytes(b"synthetic PDF")

        def converter(args, **kwargs):
            self.assertNotIn("capture_output", kwargs)
            self.assertEqual(kwargs["timeout"], 120)
            self.assertNotIn("SECRET_TOKEN", kwargs["env"])
            self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
            Path(args[-1]).write_bytes(b"x" * 33)
            return subprocess.CompletedProcess(args, 0)

        with mock.patch.object(engine.shutil, "which", return_value="pdftotext"), mock.patch.dict(
            os.environ, {"SECRET_TOKEN": "SECRET"}), mock.patch.object(engine, "MAX_PDF_TEXT_BYTES", 32), mock.patch.object(
            engine.subprocess, "run", side_effect=converter):
            self.assertEqual(engine.extract_text(pdf), ("", "pdf-extraction-too-large"))
        with mock.patch.object(engine.shutil, "which", return_value="pdftotext"), mock.patch.object(
            engine.subprocess, "run", side_effect=subprocess.TimeoutExpired("pdftotext", 120)):
            self.assertEqual(engine.extract_text(pdf), ("", "pdf-extraction-timeout"))

    def test_unc_engine_and_storage_entrypoints_reject_before_filesystem(self):
        unsafe = (r"\\host\share\file", "//host/share/file", r"\\?\UNC\host\share\file",
                  r"\\.\pipe\name", r"\??\C:\file", r"\/host\share\file", "file://host/share/file")

        def batch(path):
            with storage.batch_durable_sync(Path(path)):
                self.fail("entered unsafe batch")

        calls = {
            "require_project": lambda p: engine.require_project(p),
            "init_root": lambda p: engine.init_project(p, "unc-test", "topic", allow_single_copy=True),
            "init_mirror": lambda p: engine.init_project(self.root, "unc-test", "topic", mirror_root=p),
            "init_import_roots": lambda p: engine.init_project(self.root, "unc-test", "topic", import_roots=[p]),
            "ingest_file": lambda p: engine.ingest_file(self.project, p),
            "extract_text": lambda p: engine.extract_text(Path(p)),
            "extract_docx": lambda p: engine.extract_docx(Path(p)),
            "import_roots": lambda p: engine.allowed_import_roots(Path(p)),
            "metadata": lambda p: engine.read_project_metadata(Path(p)),
            "connect": lambda p: engine.connect(Path(p)),
            "retention": lambda p: engine.prepare_ebook_retention(self.project, p),
            "restore_source": lambda p: engine.restore_knowledge_base(p, self.root),
            "restore_destination": lambda p: engine.restore_knowledge_base(self.project, p),
            "reject_links": lambda p: storage.reject_links(Path(p)),
            "mirror_root": lambda p: storage.mirror_project_path(self.project, p, "unc-test"),
            "atomic_write": lambda p: storage.atomic_write_bytes(Path(p), b"x"),
            "copy_source": lambda p: storage.atomic_copy(Path(p), self.root / "out"),
            "copy_destination": lambda p: storage.atomic_copy(self.root / "in", Path(p)),
            "backup_source": lambda p: storage.backup_sqlite(Path(p), self.root / "out"),
            "backup_destination": lambda p: storage.backup_sqlite(self.root / "in", Path(p)),
            "sync": lambda p: storage.sync_project_mirror(Path(p)),
            "sync_relative": lambda p: storage.sync_project_mirror(self.project, [p]),
            "hash": lambda p: storage.sha256_file(Path(p)),
            "fsync": lambda p: storage._fsync_directory(Path(p)),
            "remove": lambda p: storage.guarded_remove_project(Path(p), "uuid"),
            "restore_storage_source": lambda p: storage.restore_project_from_mirror(Path(p), self.root),
            "restore_storage_destination": lambda p: storage.restore_project_from_mirror(self.project, Path(p)),
            "batch": batch,
        }
        with ExitStack() as stack:
            traps = [stack.enter_context(mock.patch.object(Path, name,
                side_effect=AssertionError(f"{name} before validation"))) for name in ("resolve", "stat", "open")]
            traps.append(stack.enter_context(mock.patch.object(storage.os, "open", side_effect=AssertionError("OS open"))))
            for path in unsafe:
                for name, call in calls.items():
                    with self.subTest(path=path, entry=name), self.assertRaisesRegex(ValueError, "local path"):
                        call(path)
            for trap in traps:
                trap.assert_not_called()

    def test_metadata_and_expanded_home_checked_before_resolve(self):
        for field, value in (("mirror_root", "//host/share"), ("import_roots", ["local", "//host/share"])):
            with self.subTest(field=field), mock.patch.object(Path, "read_text", return_value=json.dumps({field: value})), mock.patch.object(
                Path, "resolve", side_effect=AssertionError("resolve before metadata validation")) as resolve:
                with self.assertRaisesRegex(ValueError, "local path"):
                    engine.read_project_metadata(self.project)
                resolve.assert_not_called()
        with mock.patch.object(engine, "read_project_metadata", return_value={"import_roots": ["local", "//host/share"]}), mock.patch.object(
            Path, "resolve", side_effect=AssertionError("resolve before import root validation")) as resolve:
            with self.assertRaisesRegex(ValueError, "local path"):
                engine.allowed_import_roots(self.project)
            resolve.assert_not_called()
        with mock.patch.object(Path, "expanduser", return_value=Path("//host/share")), mock.patch.object(
            Path, "resolve", side_effect=AssertionError("resolve after unsafe expansion")) as resolve:
            with self.assertRaisesRegex(ValueError, "local path"):
                engine.require_project("~/project")
            resolve.assert_not_called()

    def test_purge_release_path_from_database_checked_before_resolve(self):
        from datetime import datetime, timedelta, timezone
        with closing(engine.connect(self.project)) as conn:
            conn.execute("INSERT INTO finalizations VALUES (?,?,?,?,?,?,?)", (
                "F-unc", "//host/share/ebook.md", "digest", engine.sha256_text("token"),
                engine.now_iso(), (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "prepared"))
            conn.commit()
        with mock.patch.object(engine, "require_project", return_value=self.project), mock.patch.object(
            engine, "verify_knowledge_base", return_value={"pass": True}), mock.patch.object(
            Path, "resolve", side_effect=AssertionError("release resolved before validation")) as resolve:
            with self.assertRaisesRegex(ValueError, "local path"):
                engine.purge_knowledge_base(self.project, "token", "irrelevant")
            resolve.assert_not_called()

    def test_posix_local_path_syntax_remains_accepted(self):
        from ebe.isolation import validate_local_path
        for value in ("/tmp/ebe/project", "relative/project", "./local.txt"):
            self.assertEqual(validate_local_path(value), value)
            self.assertEqual(storage.local_path(value), Path(value))
        resolved = Path("/tmp/ebe/resolved")
        with mock.patch.object(Path, "resolve", return_value=resolved) as resolve, mock.patch.object(Path, "is_file", return_value=True):
            self.assertEqual(engine.require_project("/tmp/ebe/project"), resolved)
            resolve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
