"""Temporary projects and mocked generate only; no model/server/network calls."""
from contextlib import closing
import hashlib
from http.client import IncompleteRead
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ebe import review_service as service
from ebe.core import modules


class ReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine, _, _ = modules()
        self.engine.init_project(self.temp.name, "review-test", "Offline review", allow_single_copy=True)
        self.project = Path(self.temp.name) / "review-test"
        self.claims = [{"id": "C1", "text": "Evidence supports claim", "risk": "critical", "source_ids": ["a", "b", "c"]}]
        with closing(self.engine.connect(self.project)) as conn:
            for sid in "abc":
                body = f"Evidence from source {sid}: a sufficiently long exact reviewed supporting excerpt.\r\n" + "context " * 1000
                (self.project / f"{sid}.txt").write_bytes(body.encode())
                meta = {"evidence_review": {"relevant": True, "grade": "strong", "excerpt": body[:80]}}
                conn.execute("INSERT INTO sources(source_id,title,source_type,status,publisher,text_path,text_sha256,metadata_json) VALUES (?,?,?,?,?,?,?,?)",
                             (sid, sid, "academic", "ingested", sid, f"{sid}.txt", hashlib.sha256(body.encode()).hexdigest(), json.dumps(meta)))
            conn.commit()
        self.mock = patch.object(service.local_model, "generate", side_effect=self.support).start()
        self.addCleanup(patch.stopall)

    def support(self, request, **kwargs):
        p = request["packet"]
        self.assertTrue(all("body" not in r for r in p["sources"]))
        return {"content": "Excerpt review only.", "metadata": {
            "claim_id": p["claim"]["id"], "cache_key": p["cache_key"], "status": "support",
            "citations": [{"source_id": s["source_id"], "quote": s["excerpts"][0]["text"], "stance": "support"}
                          for s in p["sources"]]}}

    def run_review(self, **kwargs):
        return service.run_review(self.project, self.claims, kwargs.pop("model", "local@v1"),
                                  kwargs.pop("base_url", "http://127.0.0.1:8000/v1"), **kwargs)

    def cache(self):
        return next((self.project / "outputs/review-cache").glob("*.json"))

    def test_persist_and_revalidate_cache(self):
        first = self.run_review()
        self.assertTrue(first["required_readiness"]["pass"])
        self.assertFalse(first["semantic_truth_verified"])
        self.assertEqual(first["artifact"]["artifact_type"], "cross-review")
        second = self.run_review()
        self.assertEqual(self.mock.call_count, 1)
        self.assertTrue(second["attempts"][0]["cache_hit"])
        self.assertGreater(second["artifact"]["version"], first["artifact"]["version"])
        with closing(self.engine.connect(self.project)) as conn:
            meta = json.loads(conn.execute("SELECT metadata_json FROM artifacts WHERE artifact_id=?", (second["artifact"]["artifact_id"],)).fetchone()[0])
        for field in ("plan", "results", "required_readiness"):
            self.assertEqual(meta[field], second[field])
        self.assertEqual(list((self.project / "outputs/review-cache").glob("*.part")), [])

    def test_model_and_service_version_invalidate(self):
        self.run_review()
        self.run_review(model="local@v2")
        with patch.object(service, "SERVICE_VERSION", "v-next"):
            self.run_review()
        self.assertEqual(self.mock.call_count, 3)

    def test_source_change_invalidates_even_outside_excerpt(self):
        first = self.run_review()
        path = self.project / "a.txt"
        body = path.read_bytes() + b" changed outside excerpt"
        path.write_bytes(body)
        with closing(self.engine.connect(self.project)) as conn:
            conn.execute("UPDATE sources SET text_sha256=? WHERE source_id='a'", (hashlib.sha256(body).hexdigest(),))
            conn.commit()
        second = self.run_review()
        self.assertEqual(self.mock.call_count, 2)
        self.assertNotEqual(first["results"][0]["cache_key"], second["results"][0]["cache_key"])

    def test_bad_cache_plus_model_failure_never_uses_old_report(self):
        self.run_review()
        self.cache().write_text("broken", encoding="utf-8")
        self.mock.side_effect = TimeoutError("local model timeout")
        report = self.run_review()
        self.assertFalse(report["required_readiness"]["pass"])
        self.assertEqual(report["results"], [])
        self.assertTrue(report["needs_escalation"])
        self.assertIn("cache_error", report["attempts"][0])

    def test_bad_cache_is_repaired_only_by_fresh_valid_review(self):
        self.run_review()
        cache = json.loads(self.cache().read_text())
        cache["result"]["citations"][0]["quote"] = "fabricated"
        self.cache().write_text(json.dumps(cache))
        report = self.run_review()
        self.assertEqual(self.mock.call_count, 2)
        self.assertTrue(report["required_readiness"]["pass"])
        self.assertFalse(report["attempts"][0]["cache_hit"])

    def test_wrong_model_cache_binding_is_not_trusted(self):
        self.run_review()
        cache = json.loads(self.cache().read_text())
        cache["binding"]["model"] = "other"
        self.cache().write_text(json.dumps(cache))
        self.mock.side_effect = OSError("unavailable")
        self.assertFalse(self.run_review()["required_readiness"]["pass"])

    def test_conflict_persists_but_escalates_on_cache_hit(self):
        def conflict(request, **kw):
            response = self.support(request, **kw)
            response["metadata"]["status"] = "conflict"
            response["metadata"]["citations"][0]["stance"] = "conflict"
            return response
        self.mock.side_effect = conflict
        for _ in range(2):
            report = self.run_review()
            self.assertFalse(report["required_readiness"]["pass"])
            self.assertTrue(report["needs_escalation"])
        self.assertEqual(self.mock.call_count, 1)

    def test_invalid_strict_model_metadata_not_cached(self):
        def bad(request, **kw):
            response = self.support(request, **kw)
            response["metadata"]["verified"] = True
            return response
        self.mock.side_effect = bad
        report = self.run_review()
        self.assertFalse(report["required_readiness"]["pass"])
        self.assertEqual(report["results"], [])
        self.assertFalse((self.project / "outputs/review-cache").exists())

    def test_cache_write_failure_not_pass(self):
        original = self.engine.atomic_write_text
        def write(path, data):
            if path.parent.name == "review-cache":
                raise OSError("disk full")
            return original(path, data)
        with patch.object(self.engine, "atomic_write_text", side_effect=write):
            report = self.run_review()
        self.assertFalse(report["required_readiness"]["pass"])
        self.assertEqual(report["results"], [])

    def test_unreviewed_sources_and_zero_budget(self):
        report = self.run_review(budget=0)
        self.assertFalse(report["required_readiness"]["pass"])
        self.mock.assert_not_called()
        with closing(self.engine.connect(self.project)) as conn:
            conn.execute("UPDATE sources SET metadata_json='{}'")
            conn.commit()
        report = self.run_review()
        self.assertFalse(report["required_readiness"]["pass"])
        self.mock.assert_not_called()

    def test_changed_source_during_generation_is_not_ready(self):
        def change(request, **kw):
            response = self.support(request, **kw)
            with closing(self.engine.connect(self.project)) as conn:
                conn.execute("UPDATE sources SET publisher='new-owner' WHERE source_id='a'")
                conn.commit()
            return response
        self.mock.side_effect = change
        report = self.run_review()
        self.assertFalse(report["required_readiness"]["pass"])
        self.assertEqual(report["results"], [])

    def test_bad_hash_and_path_rejected_before_model(self):
        with closing(self.engine.connect(self.project)) as conn:
            conn.execute("UPDATE sources SET text_sha256='bad' WHERE source_id='a'")
            conn.commit()
        with self.assertRaises(ValueError):
            self.run_review()
        with closing(self.engine.connect(self.project)) as conn:
            conn.execute("UPDATE sources SET text_path='../escape.txt' WHERE source_id='a'")
            conn.commit()
        with self.assertRaises(ValueError):
            self.run_review()
        self.mock.assert_not_called()

    def test_configuration_rejected_even_before_cache(self):
        for kw in ({"model": ""}, {"base_url": "http://example.com:80"}, {"budget": True}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.run_review(**kw)
        self.mock.assert_not_called()

    def test_failed_packet_does_not_skip_later_packets(self):
        self.claims.append(dict(self.claims[0], id="C2"))
        def partial(request, **kw):
            if request["packet"]["claim"]["id"] == "C1":
                raise IncompleteRead(b"partial")
            return self.support(request, **kw)
        self.mock.side_effect = partial
        report = self.run_review(budget=24000)
        self.assertEqual(self.mock.call_count, 2)
        self.assertEqual([r["claim_id"] for r in report["results"]], ["C2"])
        self.assertFalse(report["required_readiness"]["pass"])
        self.assertTrue(report["needs_escalation"])

    def test_insufficient_result_is_retained_without_pass(self):
        def insufficient(request, **kw):
            response = self.support(request, **kw)
            response["metadata"].update(status="insufficient", citations=[])
            return response
        self.mock.side_effect = insufficient
        report = self.run_review()
        self.assertEqual(report["results"][0]["status"], "insufficient")
        self.assertFalse(report["required_readiness"]["pass"])
        self.assertTrue(report["needs_escalation"])


if __name__ == "__main__":
    unittest.main()
