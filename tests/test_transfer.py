"""Collector/core boundary tests. All fetches and sockets are offline mocks."""
from __future__ import annotations

import hashlib
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from collections import Counter
from contextlib import closing, ExitStack
from pathlib import Path
from unittest import mock

from ebe import transfer
from ebe.core import modules


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine, self.research, _ = modules()
        self.project = Path(self.engine.init_project(self.root / "primary", "transfer-test", "Synthetic topic",
                             mirror_root=self.root / "mirror")["project"])
        self.manifest = self.root / "requests.json"
        self.output = self.root / "collected"
        self.enterContext(mock.patch.object(socket, "socket", side_effect=AssertionError("real socket forbidden")))
        self.enterContext(mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("real DNS forbidden")))
        self.fetch = self.enterContext(mock.patch.object(transfer.network, "fetch", side_effect=self.body))

    @staticmethod
    def body(url):
        return ((url + " Synthetic source evidence, method and limitations. " * 30).encode(), "text/plain", url)

    def seed(self, count=2):
        query = self.research.plan_research(str(self.project), queries=[{
            "query": "synthetic", "dimension": "audience", "provider": "codex"}])["query_ids"][0]
        self.research.submit_search_results(str(self.project), query, 0,
            [{"url": f"https://d{i % 2}.example/{i}"} for i in range(count)])
        transfer.export_requests(self.project, self.manifest)
        return json.loads(self.manifest.read_text())["requests"]

    def collect(self, **kwargs):
        return transfer.collect(self.manifest, self.output, **kwargs)

    def receipt(self):
        return json.loads((self.output / "bundle.json").read_text())

    def save_receipt(self, receipt):
        (self.output / "bundle.json").write_text(json.dumps(receipt), encoding="utf-8")

    def states(self):
        with closing(self.research.connect(self.project)) as conn:
            return [(r[0], r[1]) for r in conn.execute("SELECT source_id,status FROM sources ORDER BY source_id")]

    def test_collect_resume_only_retries_failed_and_redacts_errors(self):
        requests = self.seed()

        def partial(url):
            if url == requests[1]["url"]:
                raise TimeoutError("SECRET https://user:password@private/")
            return self.body(url)

        self.fetch.side_effect = partial
        first = self.collect()
        self.assertEqual((first["downloaded"], first["failed"]), (1, 1))
        self.assertNotIn("SECRET", (self.output / "bundle.json").read_text())
        self.assertEqual(self.receipt()["results"][1]["error"], "TimeoutError")
        self.fetch.reset_mock()
        self.fetch.side_effect = self.body
        self.assertEqual(self.collect()["downloaded"], 2)
        self.fetch.assert_called_once_with(requests[1]["url"])
        self.fetch.reset_mock()
        self.collect()
        self.fetch.assert_not_called()

    def test_resume_tampered_or_missing_file_is_downloaded_again(self):
        requests = self.seed(1)
        self.collect()
        cached = self.output / self.receipt()["results"][0]["file"]
        cached.write_bytes(b"tampered")
        self.fetch.reset_mock()
        self.collect()
        self.fetch.assert_called_once_with(requests[0]["url"])
        cached.unlink()
        self.fetch.reset_mock()
        self.assertEqual(self.collect()["downloaded"], 1)
        self.fetch.assert_called_once_with(requests[0]["url"])

    def test_resume_rejects_changed_manifest_and_receipt_identity(self):
        self.seed(1)
        self.collect()
        receipt = self.receipt()
        receipt["results"][0]["url"] = "https://different.example/"
        self.save_receipt(receipt)
        self.fetch.reset_mock()
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.collect()
        self.fetch.assert_not_called()
        receipt["request_hash"] = "0" * 64
        self.save_receipt(receipt)
        with self.assertRaisesRegex(ValueError, "resume_manifest_changed"):
            self.collect()

    def test_collect_parallel_domain_limit_and_no_sqlite_in_workers(self):
        self.seed(8)
        owner = threading.get_ident()
        connect = self.engine.connect
        active = Counter()
        peaks = Counter()
        lock = threading.Lock()

        def checked_connect(project):
            self.assertEqual(threading.get_ident(), owner, "SQLite called from download thread")
            return connect(project)

        def download(url):
            host = transfer.urlsplit(url).hostname
            self.assertNotEqual(threading.get_ident(), owner)
            with lock:
                active[host] += 1
                active["total"] += 1
                peaks[host] = max(peaks[host], active[host])
                peaks["total"] = max(peaks["total"], active["total"])
            try:
                time.sleep(.03)
                return self.body(url)
            finally:
                with lock:
                    active[host] -= 1
                    active["total"] -= 1

        self.fetch.side_effect = download
        with mock.patch.object(self.engine, "connect", side_effect=checked_connect):
            self.assertEqual(self.collect(workers=4)["downloaded"], 8)
        self.assertEqual(peaks["total"], 4)
        self.assertEqual(peaks["d0.example"], 2)
        self.assertEqual(peaks["d1.example"], 2)

    def test_import_awaiting_review_single_writer_mirror_and_resume(self):
        self.seed(3)
        self.collect()
        owner = threading.get_ident()
        connect = self.engine.connect

        def checked_connect(project):
            self.assertEqual(threading.get_ident(), owner)
            return connect(project)

        self.fetch.reset_mock()
        with mock.patch.object(self.engine, "connect", side_effect=checked_connect), mock.patch.object(
            self.engine, "ingest_url", side_effect=AssertionError("offline import cannot download")):
            first = transfer.import_bundle(self.project, self.output / "bundle.json")
            self.assertEqual(len(first["imported"]), 3)
            self.assertTrue(all(r["status"] == "awaiting-review" for r in first["imported"]))
            again = transfer.import_bundle(self.project, self.output / "bundle.json")
        self.assertEqual(again, {"imported": [], "skipped_existing": 3})
        self.fetch.assert_not_called()
        self.assertEqual(self.engine.research_readiness(self.project)["qualified_sources"], 0)
        self.assertEqual(self.engine.MIN_QUALIFIED_SOURCES, 120)
        self.assertTrue(self.engine.verify_knowledge_base(self.project)["pass"])
        with closing(self.research.connect(self.project)) as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT attempts FROM acquisitions")], [1, 1, 1])

    def test_import_checks_entire_bundle_before_writes_hash_and_identity(self):
        self.seed()
        self.collect()
        original = self.receipt()
        for field, value, error in (("sha256", "0" * 64, "bundle_hash_mismatch"),
                                    ("id", "S9999", "bundle_source_mismatch"),
                                    ("url", "https://wrong.example/", "bundle_source_mismatch")):
            receipt = json.loads(json.dumps(original))
            receipt["results"][1][field] = value
            self.save_receipt(receipt)
            with self.subTest(field=field), mock.patch.object(self.engine, "ingest_saved_file") as ingest:
                with self.assertRaisesRegex(ValueError, error):
                    transfer.import_bundle(self.project, self.output / "bundle.json")
                ingest.assert_not_called()
            self.assertTrue(all(status == "selected" for _, status in self.states()))

    def test_import_path_escape_rejected_before_writes(self):
        self.seed(1)
        self.collect()
        receipt = self.receipt()
        for path in ("../outside.txt", "/outside.txt", "C:/outside.txt", "..\\outside.txt",
                     "input/../../outside.txt", "file.txt:stream"):
            receipt["results"][0]["file"] = path
            self.save_receipt(receipt)
            with self.subTest(path=path), mock.patch.object(self.engine, "ingest_saved_file") as ingest:
                with self.assertRaises(ValueError):
                    transfer.import_bundle(self.project, self.output / "bundle.json")
                ingest.assert_not_called()

    def test_budget_64_mib_checked_before_reading_overflow_item(self):
        requests = self.seed(5)
        self.output.mkdir()
        # One 13 MiB file referenced by five valid source records: no 65 MiB fixture.
        body = b"x" * (13 * 1024 * 1024)
        digest = hashlib.sha256(body).hexdigest()
        payload = self.output / "payload.txt"
        payload.write_bytes(body)
        self.save_receipt({"schema": 1, "results": [dict(item, status="downloaded", file="payload.txt",
                           sha256=digest, content_type="text/plain") for item in requests]})
        original_open = Path.open
        resolved_payload = payload.resolve()
        reads = []

        def bounded_open(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if mode == "rb" and path.resolve() == resolved_payload:
                reads.append(path)
                return io.BytesIO(body)
            return original_open(path, *args, **kwargs)

        # Portable alias fixture: exercise different lexical paths without
        # depending on Windows 8.3 names or symlink permissions on CI.
        alias = payload.parent / ".." / payload.parent.name / payload.name
        self.assertNotEqual(alias, payload)
        for args, kwargs in ((("rb",), {}), ((), {"mode": "rb"})):
            with bounded_open(alias, *args, **kwargs) as handle:
                self.assertIsInstance(handle, io.BytesIO)
            self.assertEqual(reads, [alias])
            reads.clear()

        with mock.patch.object(Path, "open", bounded_open), mock.patch.object(
            self.engine, "ingest_saved_file") as ingest:
            with self.assertRaisesRegex(ValueError, "bundle_memory_budget"):
                transfer.import_bundle(self.project, self.output / "bundle.json")
            ingest.assert_not_called()
        self.assertEqual(len(reads), 4, "overflow file must not be read into memory")

    def test_unselected_source_cannot_be_imported(self):
        self.seed(1)
        self.collect()
        with closing(self.engine.connect(self.project)) as conn:
            conn.execute("UPDATE sources SET status='candidate'")
            conn.commit()
        with self.assertRaisesRegex(ValueError, "selected"):
            transfer.import_bundle(self.project, self.output / "bundle.json")
        self.assertEqual(self.states(), [("S0001", "candidate")])

    def test_public_reference_rejects_sensitive_urls(self):
        for url in ("https://u:p@example.org/", "https://example.org/?key=SECRET",
                    "https://example.org/?sig=SECRET", "https://example.org/?token=",
                    "https://example.org/?X-Amz-Signature=SECRET"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                transfer.public_reference(url)

    def test_public_reference_and_export_strip_credential_fragment(self):
        canonical = "https://d0.example/0"
        self.assertEqual(transfer.public_reference("https://D0.EXAMPLE/0#access_token=SECRET"), canonical)
        self.seed(1)
        # Simulate an older source row containing a fragment.
        with closing(self.engine.connect(self.project)) as conn:
            conn.execute("UPDATE sources SET url=?", (canonical + "#access_token=SECRET",))
            conn.commit()
        output = self.root / "normalized.json"
        transfer.export_requests(self.project, output)
        self.assertEqual(json.loads(output.read_text())["requests"][0]["url"], canonical)
        self.assertNotIn("SECRET", output.read_text())

    def test_collect_normalizes_success_failure_hash_and_resume_urls(self):
        requests = self.seed(2)
        original = json.loads(self.manifest.read_text())
        for item in original["requests"]:
            item["url"] += "#access_token=SECRET"
        self.manifest.write_text(json.dumps(original))

        def partial(url):
            self.assertNotIn("#", url)
            if url == requests[1]["url"]:
                raise TimeoutError("SECRET")
            return self.body(url)

        self.fetch.side_effect = partial
        result = self.collect()
        self.assertEqual((result["downloaded"], result["failed"]), (1, 1))
        receipt = self.receipt()
        self.assertEqual(receipt["request_hash"], hashlib.sha256(json.dumps(requests, sort_keys=True).encode()).hexdigest())
        self.assertEqual([r["url"] for r in receipt["results"]], [r["url"] for r in requests])
        self.assertNotIn("SECRET", (self.output / "bundle.json").read_text())
        # A cached receipt must not reintroduce a fragment through the early return.
        receipt["results"][0]["url"] += "#access_token=SECRET"
        self.save_receipt(receipt)
        self.manifest.write_text(json.dumps({"schema": 1, "requests": requests}))
        self.fetch.reset_mock()
        self.fetch.side_effect = self.body
        self.assertEqual(self.collect()["downloaded"], 2)
        self.fetch.assert_called_once_with(requests[1]["url"])
        for path in self.output.iterdir():
            self.assertNotIn(b"SECRET", path.read_bytes())

    def test_import_uses_canonical_url_without_persisting_fragment(self):
        self.seed(1)
        self.collect()
        receipt = self.receipt()
        receipt["results"][0]["url"] += "#access_token=SECRET"
        self.save_receipt(receipt)
        result = transfer.import_bundle(self.project, self.output / "bundle.json")
        self.assertEqual(result["imported"][0]["status"], "awaiting-review")
        with closing(self.engine.connect(self.project)) as conn:
            self.assertNotIn("SECRET", "\n".join(conn.iterdump()))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_unc_paths_rejected_before_resolve_stat_or_open(self):
        calls = {
            "read_json": lambda p: transfer.read_json(p),
            "child_root": lambda p: transfer.safe_child(p, "body.txt"),
            "child_relative": lambda p: transfer.safe_child(self.root, p),
            "atomic_json": lambda p: transfer.atomic_json(p, {}),
            "export_project": lambda p: transfer.export_requests(p, self.manifest),
            "export_output": lambda p: transfer.export_requests(self.project, p),
            "collect_manifest": lambda p: transfer.collect(p, self.output),
            "collect_output": lambda p: transfer.collect(self.manifest, p),
            "import_project": lambda p: transfer.import_bundle(p, self.manifest),
            "import_bundle": lambda p: transfer.import_bundle(self.project, p),
        }
        with ExitStack() as stack:
            traps = [stack.enter_context(mock.patch.object(Path, name,
                side_effect=AssertionError(f"{name} before validation"))) for name in ("resolve", "stat", "open")]
            for path in (r"\\host\share\body", "//host/share/body", r"\\?\UNC\host\share", r"\??\C:\body"):
                for name, call in calls.items():
                    with self.subTest(path=path, entry=name), self.assertRaises(ValueError):
                        call(path)
            for trap in traps:
                trap.assert_not_called()
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
