from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest import mock

from ebe.core import modules
from tests.legacy.test_engine import OfflineTestCase

engine, research, pipeline = modules()


class ResearchTest(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(engine.init_project(self.temp.name, "synthetic-research", "Fictional process",
                                                allow_single_copy=True)["project"])

    def query(self, provider="codex"):
        return research.plan_research(str(self.project), queries=[{
            "query": "fictional research test", "dimension": "audience", "provider": provider}])["query_ids"][0]

    def fake_ingest(self, project, source_id):
        body = f"Distinct synthetic document {source_id}. " + ("A fictional observation explains a method, limitation and reader task. " * 20)
        path = project / "sources" / "raw" / f"{source_id}.txt"
        engine.atomic_write_text(path, body)
        with closing(engine.connect(project)) as conn:
            result = engine.ingest_saved_file(project, conn, source_id, path, "text/plain", 1800, 200)
            conn.commit()
        engine.sync_durable_state(project, [result["raw_path"], result["text_path"]])
        return result

    def test_discovery_deduplicates_tracking_and_doi_and_stale_pages(self):
        q = self.query()
        result = research.submit_search_results(str(self.project), q, 0, [
            {"url": "https://example.org/paper?utm_source=a", "doi": "https://doi.org/10.1/test"},
            {"url": "https://example.org/other", "doi": "10.1/test"},
            {"url": "https://example.org/paper?utm_source=b"},
            {"url": "file:///private"}])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["duplicates"], 2)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(research.submit_search_results(str(self.project), q, 0, [])["status"], "stale_page")

    def test_body_requires_review_and_matching_excerpt(self):
        q = self.query()
        research.submit_search_results(str(self.project), q, 0, [{"url": "https://example.org/paper"}])
        with mock.patch.object(engine, "ingest_url", side_effect=self.fake_ingest):
            result = research.run_research_batch(str(self.project), workers=1)
        self.assertEqual(result["results"][0]["status"], "awaiting-review")
        self.assertEqual(engine.research_readiness(self.project)["qualified_sources"], 0)
        body = research.read_source_body(str(self.project), "S0001", max_chars=100)
        self.assertEqual(body["next_offset"], 100)
        with self.assertRaisesRegex(ValueError, "excerpt"):
            research.review_source(str(self.project), "S0001", True, "primary", "strong", "test", "x" * 40)
        research.review_source(str(self.project), "S0001", True, "case-study", "usable-with-limits",
                               "Synthetic evidence only", body["body"][:60])
        self.assertEqual(engine.research_readiness(self.project)["qualified_sources"], 1)
        with mock.patch.object(engine, "ingest_url") as fetch:
            research.run_research_batch(str(self.project), workers=1)
            fetch.assert_not_called()

    def test_rate_limit_checkpoints_and_does_not_sleep_or_leak_key(self):
        q = self.query("brave")
        with mock.patch.object(research, "search_provider", side_effect=research.ProviderError("provider_http_429", 120)) as provider:
            first = research.run_research_batch(str(self.project))
            second = research.run_research_batch(str(self.project))
            self.assertEqual(provider.call_count, 1)
        self.assertEqual(first["searches"][0]["retry_after"], 120)
        self.assertEqual(second["searches"], [])
        self.assertEqual(research.research_status(str(self.project))["queries"][0]["query_id"], q)

    def test_provider_parsing_and_auth_headers(self):
        with mock.patch.dict("os.environ", {"BRAVE_SEARCH_API_KEY": "TEST-SECRET"}), mock.patch.object(
            research, "provider_request", return_value={"web": {"results": [{"url": "https://example.org", "title": "Test"}]},
                                                       "query": {"more_results_available": True}}) as fetch:
            response = research.search_provider("brave", "fictional", 2)
            self.assertTrue(response["has_more"])
            self.assertNotIn("TEST-SECRET", fetch.call_args.args[0])
            self.assertEqual(fetch.call_args.args[1]["X-Subscription-Token"], "TEST-SECRET")
        with mock.patch.object(research, "provider_request", return_value={"results": [
            {"title": "Paper", "doi": "https://doi.org/10.1/a", "best_oa_location": {"pdf_url": "https://example.org/a.pdf"}}],
            "meta": {"count": 22}}):
            response = research.search_provider("openalex", "fictional", 0)
        self.assertTrue(response["has_more"])
        self.assertEqual(response["candidates"][0]["url"], "https://example.org/a.pdf")

    def test_provider_error_redaction(self):
        error = urllib.error.HTTPError("https://secret.example/?key=SECRET", 429, "SECRET", {"Retry-After": "90"}, io.BytesIO(b"SECRET"))
        opener = mock.Mock()
        opener.open.side_effect = error
        with mock.patch.object(research.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(research.ProviderError) as caught:
                research.provider_request("https://example.org", {})
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(caught.exception.retry_after, 90)

    def test_duplicate_normalized_body_is_excluded(self):
        q = self.query()
        research.submit_search_results(str(self.project), q, 0,
            [{"url": "https://example.org/one"}, {"url": "https://example.org/two"}])
        for source_id in ("S0001", "S0002"):
            self.fake_ingest(self.project, source_id)
        with closing(engine.connect(self.project)) as conn:
            first = conn.execute("SELECT * FROM sources WHERE source_id='S0001'").fetchone()
            second = conn.execute("SELECT * FROM sources WHERE source_id='S0002'").fetchone()
            body = (self.project / first["text_path"]).read_text(encoding="utf-8")
            engine.atomic_write_text(self.project / second["text_path"], body.upper())
            conn.execute("UPDATE sources SET text_sha256=? WHERE source_id='S0002'", (engine.sha256_file(self.project / second["text_path"]),))
            conn.commit()
        self.assertEqual(research.assess_source(self.project, "S0001")["status"], "awaiting-review")
        self.assertEqual(research.assess_source(self.project, "S0002")["reason"], "duplicate_text")

    def test_failed_fetch_retry_and_batch_checkpoint(self):
        q = self.query()
        research.submit_search_results(str(self.project), q, 0, [{"url": "https://example.org/paper"}])
        with mock.patch.object(engine, "ingest_url", side_effect=TimeoutError):
            result = research.run_research_batch(str(self.project), workers=1)
        self.assertEqual(result["results"][0]["status"], "failed")
        research.retry_research(str(self.project))
        with mock.patch.object(engine, "ingest_url", side_effect=self.fake_ingest):
            result = research.run_research_batch(str(self.project), workers=1)
        self.assertEqual(result["results"][0]["status"], "awaiting-review")
        self.assertEqual(result["research"]["recent_batches"][0]["source_ids"], ["S0001"])

    def test_worker_cannot_execute_research_tools_or_consume_attempt(self):
        pipeline.start_pipeline(str(self.project))
        scripts = str(Path(engine.__file__).parent)
        code = ("import json,sys; sys.path.insert(0," + repr(scripts) + "); import research; "
                "r=json.load(sys.stdin); research.plan_research(r['project_dir']); "
                "print(json.dumps({'content':'Synthetic topic analysis','metadata':{}}))")
        before = pipeline.pipeline_status(str(self.project))
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("worker executed")) as spawn, \
                mock.patch.object(research, "plan_research", side_effect=AssertionError("research executed")) as plan:
            with self.assertRaisesRegex(ValueError, "fixed local model adapter"):
                pipeline.run_topic_pipeline(str(self.project), max_steps=1, worker=[sys.executable, "-c", code])
            spawn.assert_not_called()
            plan.assert_not_called()
        self.assertEqual(pipeline.pipeline_status(str(self.project)), before)
        self.assertEqual(before["tasks"][0]["attempts"], 0)
        self.assertEqual(research.research_status(str(self.project))["queries"], [])

    def test_mirror_restore_preserves_queue_and_pending_review(self):
        original = self.project
        self.project = Path(engine.init_project(Path(self.temp.name) / "primary", "durable-research", "Fictional process",
                                                mirror_root=Path(self.temp.name) / "mirror")["project"])
        q = self.query()
        research.submit_search_results(str(self.project), q, 0, [{"url": "https://example.org/paper"}])
        with mock.patch.object(engine, "ingest_url", side_effect=self.fake_ingest):
            research.run_research_batch(str(self.project), workers=1)
        restored = engine.restore_knowledge_base(Path(self.temp.name) / "mirror" / "durable-research",
                                                 Path(self.temp.name) / "restored")
        status = research.research_status(restored["project"])
        self.assertEqual(status["acquisitions"]["awaiting-review"], 1)
        self.assertEqual(status["queries"][0]["status"], "done")
        self.project = original


if __name__ == "__main__":
    unittest.main()
