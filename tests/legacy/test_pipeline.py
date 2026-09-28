from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from ebe.core import modules
from tests.legacy.test_engine import OfflineTestCase

engine, research, pipeline = modules()


class PipelineTest(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = engine.init_project(self.temp.name, "synthetic-run", "Fictional process",
                                           allow_single_copy=True)["project"]
        pipeline.start_pipeline(self.project, chapter_count=1, auto_approve=True, max_research_rounds=2)

    def submit(self, request, content="Synthetic analysis", metadata=None):
        return pipeline.run_topic_pipeline(self.project, submission={"task_id": request["task_id"],
            "ticket": request["ticket"], "result": {"content": content, "metadata": metadata or {}}})

    def test_resume_pause_and_stable_ticket(self):
        first = pipeline.run_topic_pipeline(self.project)
        repeated = pipeline.run_topic_pipeline(self.project)
        self.assertEqual(first["ticket"], repeated["ticket"])
        self.assertEqual(first["task_id"], repeated["task_id"])
        pipeline.control_pipeline(self.project, "pause")
        self.assertEqual(pipeline.run_topic_pipeline(self.project)["status"], "paused")
        pipeline.control_pipeline(self.project, "resume")
        second = self.submit(first)
        self.assertEqual(second["kind"], "search-plan")
        self.assertEqual(self.submit(second)["kind"], "research")

    def test_crash_after_artifact_commit_does_not_duplicate(self):
        request = pipeline.run_topic_pipeline(self.project)
        original = pipeline.advance
        with mock.patch.object(pipeline, "advance", side_effect=RuntimeError("simulated crash")):
            self.assertEqual(self.submit(request)["status"], "retryable_error")
        self.assertEqual(self.submit(request)["kind"], "search-plan")
        with closing(engine.connect(Path(self.project))) as conn:
            self.assertEqual(len(engine.artifact_rows(conn, "topic-analysis")), 1)
        self.assertIs(pipeline.advance, original)

    def test_exhausted_research_never_approves_uvz(self):
        r = self.submit(pipeline.run_topic_pipeline(self.project))
        r = self.submit(r)
        r = self.submit(r)
        self.assertEqual(r["kind"], "research")
        r = self.submit(r)
        self.assertEqual(r["status"], "blocked")
        with closing(engine.connect(Path(self.project))) as conn:
            self.assertIsNone(engine.latest_gate(conn, "uvz"))

    def test_lock_is_exclusive_and_released(self):
        with pipeline.project_lock(Path(self.project)):
            with self.assertRaisesRegex(ValueError, "busy"):
                pipeline.run_topic_pipeline(self.project)
        self.assertEqual(pipeline.run_topic_pipeline(self.project)["status"], "needs_work")

    def test_command_worker_rejected_without_execution_or_attempt(self):
        worker = [sys.executable, "-c", "import json,sys; r=json.load(sys.stdin); print(json.dumps({'content':r['kind'],'metadata':{}}))"]
        before = pipeline.pipeline_status(self.project)
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("worker executed")) as spawn:
            for command in (worker, []):
                with self.subTest(command=command), self.assertRaisesRegex(ValueError, "fixed local model adapter"):
                    pipeline.run_topic_pipeline(self.project, max_steps=1, worker=command)
            spawn.assert_not_called()
        self.assertEqual(pipeline.pipeline_status(self.project), before)
        self.assertEqual(before["tasks"][0]["attempts"], 0)
        self.assertEqual(pipeline.run_topic_pipeline(self.project)["kind"], "topic-analysis")

    def test_stale_delivery_does_not_consume_new_task_attempts(self):
        first = pipeline.run_topic_pipeline(self.project)
        self.submit(first)
        self.assertEqual(self.submit(first)["status"], "stale_submission")
        state = pipeline.pipeline_status(self.project)
        self.assertEqual(state["tasks"][1]["attempts"], 0)

    def test_worker_rejected_preserves_pending_ticket_and_submission(self):
        worker = [sys.executable, "-c", "import time; time.sleep(5)"]
        request = pipeline.run_topic_pipeline(self.project)
        before = pipeline.pipeline_status(self.project)
        submission = {"task_id": request["task_id"], "ticket": request["ticket"],
                      "result": {"content": "Synthetic analysis", "metadata": {}}}
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("worker executed")) as spawn:
            with self.assertRaisesRegex(ValueError, "fixed local model adapter"):
                pipeline.run_topic_pipeline(self.project, worker=worker, timeout=1, submission=submission)
            spawn.assert_not_called()
        self.assertEqual(pipeline.pipeline_status(self.project), before)
        self.assertEqual(before["tasks"][0]["attempts"], 0)
        self.assertEqual(pipeline.run_topic_pipeline(self.project)["ticket"], request["ticket"])
        self.assertEqual(pipeline.run_topic_pipeline(self.project, submission=submission)["kind"], "search-plan")

    def test_manual_gate_waits_without_consuming_attempts(self):
        project = Path(self.project)
        state = pipeline.load(project)
        state["config"]["auto_approve"] = False
        state["tasks"] = [pipeline.task("gate-uvz")]
        pipeline.persist(project, state)
        engine.save_artifact(project, "uvz-analysis", "Synthetic UVZ", "ready")
        self.assertEqual(pipeline.run_topic_pipeline(self.project)["status"], "needs_approval")
        self.assertEqual(pipeline.pipeline_status(self.project)["tasks"][0]["attempts"], 0)

    def test_full_120_source_execution(self):
        p = Path(self.project)
        ids = []
        for n in range(120):
            source = p / "input" / f"source-{n}.txt"
            source.write_text(f"Fictional evidence {n}: a distinct observation for synthetic testing.", encoding="utf-8")
            ids.append(engine.ingest_file(p, source)["source_id"])
        self.finish_pipeline(ids)

    def test_full_discovery_acquisition_review_to_ebook(self):
        project = Path(self.project)
        query = research.plan_research(self.project, queries=[{"query": "fictional study", "dimension": "audience", "provider": "codex"}])["query_ids"][0]

        def acquire(url, **kwargs):
            body = f"Synthetic record {url}. " + "Fictional observations support a practice and document its limitations. " * 20
            return body.encode("utf-8"), "text/plain", url

        ids = []
        with mock.patch("ebe.network.fetch", side_effect=acquire) as fetch:
            for page in range(6):
                research.submit_search_results(self.project, query, page,
                    [{"url": f"https://example.org/study/{page * 20 + n}"} for n in range(20)], has_more=page < 5)
                batch = research.run_research_batch(self.project, max_queries=0)
                self.assertEqual(len(batch["acquired_source_ids"]), 20)
                for source_id in batch["acquired_source_ids"]:
                    body = research.read_source_body(self.project, source_id)["body"]
                    research.review_source(self.project, source_id, True, "primary", "strong", "Synthetic test evidence", body[:100])
                    ids.append(source_id)
            self.assertEqual(fetch.call_count, 120)
        self.assertEqual(engine.research_readiness(project)["qualified_sources"], 120)
        self.finish_pipeline(ids)

    def finish_pipeline(self, ids):
        p = Path(self.project)
        report = {"coverage": {d: ids[:3] for d in engine.RESEARCH_DIMENSIONS},
                  "high_impact_claims": [{"claim_id": "C1", "source_ids": ids[:2]}],
                  "recent_batches": [{"source_ids": batch, "new_material_claims": 0,
                      "material_claims_before": 5, "uvz_changed": False, "promise_changed": False}
                      for batch in (ids[-40:-20], ids[-20:])]}
        r = pipeline.run_topic_pipeline(self.project)
        for _ in range(30):
            if r["status"] == "complete":
                break
            self.assertEqual(r["status"], "needs_work", r)
            kind = r["kind"]
            metadata = report if kind == "research" else {"pass": True} if kind == "editorial-review" else {}
            content = json.dumps(report) if kind == "research" else f"# {kind}\n\nFictional synthesis."
            if kind == "chapter":
                content = "# Chapter 1\n\nA fictional observation supports this illustrative procedure [S0001]."
                metadata = {"source_ids": ["S0001"]}
            r = self.submit(r, content, metadata)
        self.assertEqual(r["status"], "complete", r)
        self.assertTrue((p / "outputs" / "ebook.md").is_file())


if __name__ == "__main__":
    unittest.main()
