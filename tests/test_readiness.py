"""Synthetic SQLite fixtures only: no providers, network or private projects."""
import copy
from datetime import datetime, timezone, timedelta
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ebe" / "legacy"))
from ebe.readiness import install, required_reviews_readiness, strict_readiness_from_connection

# Separate module object: installing this fixture must not poison legacy tests.
spec = importlib.util.spec_from_file_location("readiness_fixture_engine", ROOT / "ebe/legacy/engine.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


def today():
    return datetime.now(timezone.utc).date()


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.conn = sqlite3.connect(self.project / "state.sqlite3")
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        legacy.init_schema(self.conn)
        self.conn.execute("CREATE TABLE acquisition_batches(batch_id INTEGER PRIMARY KEY, source_ids TEXT)")
        self.engine = types.SimpleNamespace(
            research_readiness_from_connection=legacy.research_readiness_from_connection,
            RESEARCH_DIMENSIONS=legacy.RESEARCH_DIMENSIONS,
            SATURATION_BATCH_SIZE=legacy.SATURATION_BATCH_SIZE,
            MAX_NEW_CLAIM_RATE=legacy.MAX_NEW_CLAIM_RATE)
        for i in range(120):
            sid = f"S{i:04d}"
            body = f"Research source {i}: a sufficiently long exact evidence excerpt for offline review."
            (self.project / f"{sid}.txt").write_text(body, encoding="utf-8")
            meta = {"evidence_review": {"relevant": True, "grade": "strong", "role": "primary", "excerpt": body}}
            self.conn.execute("INSERT INTO sources(source_id,title,source_type,status,sha256,text_path,publisher,published_at,metadata_json) VALUES (?,?,?,?,?,?,?,?,?)",
                              (sid, sid, "academic", "ingested", f"raw-{i}", f"{sid}.txt", f"publisher-{i}", today().isoformat(), json.dumps(meta)))
        batches = []
        for bid, start in ((1, 80), (2, 100)):
            ids = [f"S{i:04d}" for i in range(start, start + 20)]
            self.conn.execute("INSERT INTO acquisition_batches VALUES (?,?)", (bid, json.dumps(ids)))
            batches.append(dict(batch_id=bid, source_ids=ids, new_material_claims=0,
                                material_claims_before=100, uvz_changed=0, promise_changed=0))
        self.meta = {"coverage": {d: ["S0000", "S0001", "S0002"] for d in legacy.RESEARCH_DIMENSIONS},
                     "high_impact_claims": [{"claim_id": "C1", "source_ids": ["S0000", "S0001"],
                                             "text": "Synthetic evidence supports this research claim.",
                                             "risk": "high", "authoritative_source_ids": ["S0000"]}],
                     "recent_batches": batches}
        self.conn.execute("INSERT INTO artifacts(artifact_id,artifact_type,version,status,file_path,sha256,created_at,metadata_json) VALUES ('A1','saturation-report',1,'ready','unused','unused','2026-01-01',?)", (json.dumps(self.meta),))
        self.save_review()

    def save_review(self):
        from ebe import evidence, review_service, local_model
        from ebe.gates import _records, _digest, current_claims
        self.conn.execute("UPDATE artifacts SET metadata_json=? WHERE artifact_type='saturation-report'", (json.dumps(self.meta),))
        binding = {"model": "synthetic-v1", "endpoint": "http://127.0.0.1:9999/v1/chat/completions",
                   "service_version": review_service.SERVICE_VERSION,
                   "prompt_hash": _digest([review_service._INSTRUCTIONS, local_model.SYSTEM_PROMPT])}
        claims, _ = current_claims(self.conn)
        plan = evidence.build_review_plan(_records(self.conn), claims, budget=12000,
                                         reviewer_model_version=_digest(binding), policy_version=evidence.POLICY_VERSION)
        results = [{"claim_id": p["claim"]["id"], "cache_key": p["cache_key"], "status": "support",
                    "citations": [{"source_id": s["source_id"], "quote": s["excerpts"][0]["text"], "stance": "support"}
                                  for s in p["sources"]]} for p in plan["packets"]]
        self.saved_review = dict(plan=plan, results=results, reviewer=binding)
        self.conn.execute("INSERT OR REPLACE INTO artifacts(artifact_id,artifact_type,version,status,file_path,sha256,created_at,metadata_json) VALUES ('R1','cross-review',1,'draft','unused','unused','2026-01-01',?)", (json.dumps(self.saved_review),))

    def update_saved_review(self):
        self.conn.execute("UPDATE artifacts SET metadata_json=? WHERE artifact_id='R1'", (json.dumps(self.saved_review),))

    def result(self):
        self.conn.execute("UPDATE artifacts SET metadata_json=? WHERE artifact_type='saturation-report'", (json.dumps(self.meta),))
        install(self.engine)
        return self.engine.research_readiness_from_connection(self.conn)

    def review(self, sid="S0000", **changes):
        row = self.conn.execute("SELECT metadata_json FROM sources WHERE source_id=?", (sid,)).fetchone()
        meta = json.loads(row[0])
        meta["evidence_review"].update(changes)
        self.conn.execute("UPDATE sources SET metadata_json=? WHERE source_id=?", (json.dumps(meta), sid))

    def test_valid_and_install_idempotent(self):
        self.assertTrue(self.result()["pass"])
        wrapped = self.engine.research_readiness_from_connection
        install(self.engine)
        self.assertIs(wrapped, self.engine.research_readiness_from_connection)
        self.assertEqual(self.result()["strict_readiness"]["qualified_sources"], 120)

    def test_direct_legacy_compatible_unreviewed_ingest_blocked(self):
        self.conn.execute("UPDATE sources SET metadata_json='{}'")
        self.assertTrue(legacy.research_readiness_from_connection(self.conn)["pass"])
        self.assertFalse(self.result()["pass"])

    def test_review_fields(self):
        for changes in ({"relevant": 1}, {"relevant": False}, {"grade": "weak"},
                        {"excerpt": "invented " * 10}, {"excerpt": "short"}):
            with self.subTest(changes=changes):
                self.review(**changes)
                self.assertFalse(self.result()["pass"])
                self.review(relevant=True, grade="strong", excerpt=(self.project / "S0000.txt").read_text())

    def test_normalized_body_duplicates(self):
        body = (self.project / "S0000.txt").read_text().upper().replace(" ", "  ")
        (self.project / "S0001.txt").write_text(body)
        self.review("S0001", excerpt=body)
        result = self.result()
        self.assertFalse(result["pass"])
        self.assertEqual(result["strict_readiness"]["qualified_sources"], 119)

    def test_same_publisher(self):
        self.conn.execute("UPDATE sources SET publisher='same'")
        self.assertFalse(self.result()["pass"])

    def test_doi_and_owner_transitive_bridge(self):
        for sid, extra in (("S0000", {"doi": "https://doi.org/10.1/ABC"}),
                           ("S0001", {"doi": "doi:10.1/abc", "owner": "Org X"}),
                           ("S0002", {"owner": "org x"})):
            meta = json.loads(self.conn.execute("SELECT metadata_json FROM sources WHERE source_id=?", (sid,)).fetchone()[0])
            meta.update(extra)
            self.conn.execute("UPDATE sources SET metadata_json=? WHERE source_id=?", (json.dumps(meta), sid))
        result = self.result()
        self.assertFalse(result["pass"])
        groups = result["strict_readiness"]["provenance_groups"]
        self.assertEqual(groups["S0000"], groups["S0002"])

    def test_hostname_merges_despite_different_publishers(self):
        for i in range(3):
            self.conn.execute("UPDATE sources SET url=? WHERE source_id=?", (f"https://sub{i}.example.org/article", f"S{i:04d}"))
        self.assertFalse(self.result()["pass"])

    def test_authority_role_and_date(self):
        self.review(role="secondary")
        self.assertFalse(self.result()["pass"])
        self.review(role="primary")
        for value in ("invalid", None, (today() + timedelta(days=1)).isoformat(),
                      (today() - timedelta(days=366)).isoformat()):
            self.conn.execute("UPDATE sources SET published_at=? WHERE source_id='S0000'", (value,))
            self.assertFalse(self.result()["pass"])
        self.conn.execute("UPDATE sources SET published_at=? WHERE source_id='S0000'", ((today() - timedelta(days=91)).isoformat(),))
        self.assertTrue(self.result()["pass"])
        self.meta["high_impact_claims"][0]["risk"] = "rapidly-changing"
        self.assertFalse(self.result()["pass"])

    def test_batch_fields_cannot_default_or_coerce(self):
        good = copy.deepcopy(self.meta["recent_batches"])
        for field in ("new_material_claims", "material_claims_before", "uvz_changed", "promise_changed", "batch_id"):
            for value in (None, False, True, "0", -1, 0.0):
                with self.subTest(field=field, value=value):
                    self.meta["recent_batches"] = copy.deepcopy(good)
                    if value is None:
                        del self.meta["recent_batches"][0][field]
                    else:
                        self.meta["recent_batches"][0][field] = value
                    self.assertFalse(self.result()["pass"])

    def test_batch_linkage_and_recency(self):
        self.meta["recent_batches"][0]["source_ids"][0] = "S0000"
        self.assertFalse(self.result()["pass"])
        self.meta["recent_batches"][0]["source_ids"][0] = "S0080"
        self.conn.execute("INSERT INTO acquisition_batches VALUES (3, '[]')")
        self.assertFalse(self.result()["pass"])

    def test_missing_batch_table_fails_closed(self):
        self.conn.execute("DROP TABLE acquisition_batches")
        self.assertFalse(self.result()["pass"])

    def test_body_missing_and_traversal(self):
        self.conn.execute("UPDATE sources SET text_path='../outside.txt' WHERE source_id='S0000'")
        self.assertFalse(self.result()["pass"])

    def test_original_failure_is_retained(self):
        self.engine.research_readiness_from_connection = lambda conn: {"pass": False, "failures": ["legacy sentinel"]}
        result = self.result()
        self.assertTrue(result["strict_readiness"]["pass"])
        self.assertFalse(result["pass"])
        self.assertIn("legacy sentinel", result["failures"])

    def test_malformed_json_fails_closed(self):
        self.conn.execute("UPDATE sources SET metadata_json='broken' WHERE source_id='S0000'")
        self.assertFalse(self.result()["pass"])

    def test_sparse_required_reviews(self):
        from ebe.evidence import build_review_plan, validate_review
        records = [{"source_id": str(i), "body": f"body {i}", "publisher": str(i), "status": "ingested"} for i in range(2)]
        plan = build_review_plan(records, [{"id": "C", "risk": "critical", "text": "claim", "source_ids": ["0", "1"]}])
        packet = plan["packets"][0]
        result = {"claim_id": "C", "cache_key": packet["cache_key"], "status": "support",
                  "citations": [{"source_id": str(i), "quote": f"body {i}", "stance": "support"} for i in range(2)]}
        self.assertTrue(required_reviews_readiness(plan, [result], validate_review)["pass"])
        self.assertFalse(required_reviews_readiness(plan, [], validate_review)["pass"])
        result["cache_key"] = "stale"
        self.assertFalse(required_reviews_readiness(plan, [result], validate_review)["pass"])

    def test_missing_review_blocks_wrapper(self):
        self.conn.execute("DELETE FROM artifacts WHERE artifact_type='cross-review'")
        self.assertFalse(self.result()["pass"])

    def test_claim_text_and_complete_current_claims_required(self):
        del self.meta["high_impact_claims"][0]["text"]
        self.assertFalse(self.result()["pass"])
        self.meta["high_impact_claims"][0]["claim"] = "New claim text"
        self.assertFalse(self.result()["pass"])
        self.save_review()
        self.assertTrue(self.result()["pass"])
        self.meta["high_impact_claims"].append(dict(self.meta["high_impact_claims"][0], claim_id="C2"))
        self.assertFalse(self.result()["pass"])

    def test_source_changes_invalidate_cross_review(self):
        body = (self.project / "S0000.txt").read_text() + " New evidence."
        (self.project / "S0000.txt").write_text(body)
        result = self.result()
        self.assertFalse(result["pass"])
        self.assertIn("stale", str(result["failures"]))

    def test_model_and_policy_changes_invalidate_cross_review(self):
        from ebe import evidence
        with mock.patch.object(evidence, "POLICY_VERSION", "future-policy"):
            self.assertFalse(self.result()["pass"])
        self.saved_review["reviewer"]["model"] = "synthetic-v2"
        self.update_saved_review()
        self.assertFalse(self.result()["pass"])
        self.save_review()
        (self.project / "project.json").write_text(json.dumps({"cross_review_reviewer": {
            "model": "configured-v2", "endpoint": self.saved_review["reviewer"]["endpoint"]}}))
        self.assertFalse(self.result()["pass"])

    def test_sampled_ordinary_insufficient_escalates(self):
        self.meta["claims"] = [{"id": "ordinary", "text": "Ordinary statement", "risk": "normal", "source_ids": ["S0000", "S0001"]}]
        self.save_review()
        self.assertTrue(self.result()["pass"])
        item = next(r for r in self.saved_review["results"] if r["claim_id"] == "ordinary")
        item.update(status="insufficient", citations=[])
        self.update_saved_review()
        self.assertFalse(self.result()["pass"])

    def test_saved_review_claim_states_cannot_omit_claim(self):
        self.saved_review["plan"]["claims"] = []
        self.saved_review["required_readiness"] = {"pass": True}
        self.update_saved_review()
        self.assertFalse(self.result()["pass"])


class FinalGateTest(unittest.TestCase):
    save_review = ReadinessTest.save_review

    def setUp(self):
        ReadinessTest.setUp(self)
        # A new real module per fixture keeps engine global dispatch faithful
        # without patching the engine used by direct legacy callers/tests.
        spec = importlib.util.spec_from_file_location("final_fixture_engine", ROOT / "ebe/legacy/engine.py")
        self.engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.engine)
        self.engine.require_project = lambda path: Path(path)
        self.engine.sync_durable_state = lambda *args, **kwargs: {"synthetic": True}
        (self.project / "project.json").write_text(json.dumps({"topic": "Synthetic ebook"}))
        for gate in ("uvz", "outline"):
            self.conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)", (gate, gate, "approved", "A1", "synthetic", "2026-01-01"))
        self.conn.commit()
        self.engine.save_artifact(self.project, "chapter", "## Chapter one\n\nSynthetic finding [S0000] [S0001].", status="ready",
                                  metadata={"chapter_number": 1, "source_ids": ["S0000", "S0001"]})
        self.engine.assemble_ebook(self.project)
        install(self.engine)

    def qa(self):
        report = self.engine.quality_check(self.project)
        self.assertTrue(report["pass"], report)
        return report["artifact"]["artifact_id"]

    def test_strict_end_to_end(self):
        """120 reviewed sources -> real run_review -> assembly -> QA -> final.

        Only model generation is mocked; planning, persisted artifacts, binding,
        strict readiness, assembly, QA and approval all execute real code.
        """
        from ebe import review_service
        from ebe.gates import current_claims
        import pipeline

        self.conn.execute("DELETE FROM artifacts WHERE artifact_type='cross-review'")
        self.conn.commit()
        self.assertTrue(getattr(self.engine.research_readiness_from_connection, "_ebe_strict_readiness", False))
        self.assertFalse(self.engine.research_readiness_from_connection(self.conn)["pass"])
        claims, high_ids = current_claims(self.conn)
        self.assertEqual({c["id"] for c in claims}, high_ids)  # complete ledger

        def deterministic_model(request, **kwargs):
            packet = request["packet"]
            return {"content": "Synthetic excerpt review: both independent records support this fixture.",
                    "metadata": {"claim_id": packet["claim"]["id"], "cache_key": packet["cache_key"],
                                 "status": "support", "citations": [
                                     {"source_id": source["source_id"], "quote": source["excerpts"][0]["text"],
                                      "stance": "support"} for source in packet["sources"]]}}

        with mock.patch.object(review_service, "modules", return_value=(self.engine, None, pipeline)), \
                mock.patch.object(review_service.local_model, "generate", side_effect=deterministic_model) as model:
            review = review_service.run_review(self.project, claims, "synthetic-immutable-v1",
                                               "http://127.0.0.1:9999/v1", budget=12000)
        self.assertEqual(model.call_count, 1)
        self.assertTrue(review["required_readiness"]["pass"])
        self.assertFalse(review["needs_escalation"])
        ready = self.engine.research_readiness_from_connection(self.conn)
        self.assertTrue(ready["pass"], ready["failures"])
        self.assertEqual(ready["strict_readiness"]["qualified_sources"], 120)
        self.assertEqual(len(set(ready["strict_readiness"]["provenance_groups"].values())), 120)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM acquisition_batches").fetchone()[0], 2)
        assembled = self.engine.assemble_ebook(self.project)
        self.assertEqual(assembled["chapters"], 1)
        report = self.engine.quality_check(self.project)
        self.assertTrue(report["pass"], report)
        result = self.engine.record_gate(self.project, "final", "approved", report["artifact"]["artifact_id"])
        self.assertEqual(result["status"], "approved")
        qa = self.conn.execute("SELECT metadata_json FROM artifacts WHERE artifact_id=?", (result["artifact_id"],)).fetchone()
        binding = json.loads(qa[0])["binding"]
        self.assertEqual(binding["ebook_sha256"], self.engine.sha256_file(self.project / "outputs/ebook.md"))
        self.assertNotEqual(result["artifact_id"], report["artifact"]["artifact_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decisions WHERE gate='final' AND status='approved'").fetchone()[0], 1)

    def test_final_reexecutes_quality_and_binds_new_qa(self):
        aid = self.qa()
        wrapped = self.engine.quality_check
        install(self.engine)
        self.assertIs(wrapped, self.engine.quality_check)
        result = self.engine.record_gate(self.project, "final", "approved", aid)
        self.assertNotEqual(result["artifact_id"], aid)
        self.assertEqual(result["requested_artifact_id"], aid)
        self.assertEqual(self.conn.execute("SELECT artifact_id FROM decisions WHERE gate='final'").fetchone()[0], result["artifact_id"])

    def test_forged_pass_is_not_final_approval(self):
        forged = self.engine.save_artifact(self.project, "quality-report", "fake", status="ready", metadata={"pass": True})
        with self.assertRaisesRegex(ValueError, "bound QA"):
            self.engine.record_gate(self.project, "final", "approved", forged["artifact_id"])

    def test_even_forged_binding_cannot_bypass_actual_quality(self):
        self.engine.save_artifact(self.project, "chapter", "## Changed\n\nTODO [S0000] [S0001].", status="ready",
                                  metadata={"chapter_number": 1, "source_ids": ["S0000", "S0001"]})
        self.engine.assemble_ebook(self.project)
        from ebe.gates import manuscript_binding
        binding = manuscript_binding(self.conn, self.engine)
        forged = self.engine.save_artifact(self.project, "quality-report", "fake", status="ready",
                                           metadata={"pass": True, "binding": binding})
        with self.assertRaisesRegex(ValueError, "fresh quality_check"):
            self.engine.record_gate(self.project, "final", "approved", forged["artifact_id"])

    def test_old_qa_artifact_refused(self):
        old = self.qa()
        self.qa()
        with self.assertRaisesRegex(ValueError, "bound QA"):
            self.engine.record_gate(self.project, "final", "approved", old)

    def test_stale_ebook_after_new_chapter(self):
        aid = self.qa()
        self.engine.save_artifact(self.project, "chapter", "## New chapter\n\nChanged finding [S0000] [S0001].", status="ready",
                                  metadata={"chapter_number": 1, "source_ids": ["S0000", "S0001"]})
        with self.assertRaisesRegex(ValueError, "stale"):
            self.engine.record_gate(self.project, "final", "approved", aid)

    def test_tampered_manuscript_refused(self):
        aid = self.qa()
        path = self.project / "outputs/ebook.md"
        path.write_text(path.read_text() + "Changed manuscript")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.engine.record_gate(self.project, "final", "approved", aid)

    def test_stale_source_and_missing_review_refused(self):
        aid = self.qa()
        path = self.project / "S0000.txt"
        original = path.read_text()
        path.write_text(original + " Updated evidence")
        with self.assertRaises(ValueError):
            self.engine.record_gate(self.project, "final", "approved", aid)
        path.write_text(original)
        self.conn.execute("DELETE FROM artifacts WHERE artifact_type='cross-review'")
        self.conn.commit()
        with self.assertRaises(ValueError):
            self.engine.record_gate(self.project, "final", "approved", aid)

    def test_reference_metadata_changed_refused(self):
        aid = self.qa()
        self.conn.execute("UPDATE sources SET title='Changed reference' WHERE source_id='S0000'")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.engine.record_gate(self.project, "final", "approved", aid)

    def test_direct_legacy_engine_not_patched(self):
        self.assertFalse(getattr(legacy.quality_check, "_ebe_bound_quality", False))
        self.assertFalse(getattr(legacy.research_readiness_from_connection, "_ebe_strict_readiness", False))


if __name__ == "__main__":
    unittest.main()
