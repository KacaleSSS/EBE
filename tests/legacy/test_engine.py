from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "ebe" / "legacy"

from ebe.core import modules

engine, _, _ = modules()


class OfflineTestCase(unittest.TestCase):
    """Per-test socket deny, restored by cleanup; no process-wide audit hook.

    Assert no attempts even when production catches the injected error. Other
    suites (isolation and loopback HTTP mocks) retain their normal sockets.
    """
    def setUp(self):
        super().setUp()
        for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname",
                     "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
            patcher = mock.patch.object(socket, name, side_effect=AssertionError(
                "legacy tests must mock network access: " + name))
            blocked = patcher.start()
            self.addCleanup(patcher.stop)
            self.addCleanup(blocked.assert_not_called)


class EngineAcceptanceTest(OfflineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.status = engine.init_project(
            self.root,
            "synthetic-project",
            "Synthetic topic for pipeline verification only",
            language="en",
            audience_hint="fictional readers",
            allow_single_copy=True,
        )
        self.project = Path(self.status["project"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def ingest_synthetic_sources(self, count: int = engine.MIN_QUALIFIED_SOURCES) -> dict[str, object]:
        first: dict[str, object] | None = None
        for index in range(1, count + 1):
            source = self.project / "input" / f"synthetic-source-{index:03d}.txt"
            source.write_text(
                f"Synthetic evidence document {index}. A small repeatable method helps fictional readers "
                "complete a fictional task. This file exists only to test retrieval, source counting, and citations.",
                encoding="utf-8",
            )
            ingested = engine.ingest_file(
                self.project,
                source,
                title=f"Synthetic evidence {index}",
                source_type="synthetic",
            )
            if first is None:
                first = ingested
        assert first is not None
        return first

    def save_passing_saturation_report(self) -> dict[str, object]:
        source_ids = [f"S{index:04d}" for index in range(1, engine.MIN_QUALIFIED_SOURCES + 1)]
        metadata = {
            "format": "json",
            "coverage": {dimension: source_ids[:3] for dimension in engine.RESEARCH_DIMENSIONS},
            "high_impact_claims": [
                {"claim_id": "C-001", "source_ids": source_ids[:2], "risk": "normal"}
            ],
            "recent_batches": [
                {
                    "source_ids": source_ids[-40:-20],
                    "new_material_claims": 2,
                    "material_claims_before": 40,
                    "uvz_changed": False,
                    "promise_changed": False,
                },
                {
                    "source_ids": source_ids[-20:],
                    "new_material_claims": 2,
                    "material_claims_before": 60,
                    "uvz_changed": False,
                    "promise_changed": False,
                },
            ],
        }
        return engine.save_artifact(
            self.project,
            "saturation-report",
            json.dumps(metadata, indent=2),
            status="ready",
            metadata=metadata,
        )

    def test_complete_synthetic_pipeline(self) -> None:
        ingested = self.ingest_synthetic_sources()
        self.assertEqual(ingested["source_id"], "S0001")
        self.assertEqual(ingested["status"], "ingested")

        retrieved = engine.search_corpus(self.project, "repeatable fictional method")
        self.assertEqual(retrieved["results"][0]["source_id"], "S0001")

        search_plan = engine.save_artifact(self.project, "search-plan", "# Synthetic search plan")
        engine.save_artifact(self.project, "evidence-map", "# Evidence\n\nC-001 -> [S0001]")
        uvz_v1 = engine.save_artifact(
            self.project, "uvz-analysis", "# UVZ\n\nSynthetic candidate.", status="ready"
        )
        uvz_v2 = engine.save_artifact(
            self.project, "uvz-analysis", "# UVZ\n\nRevised synthetic candidate.", status="ready"
        )
        self.assertEqual(uvz_v1["version"], 1)
        self.assertEqual(uvz_v2["version"], 2)
        self.save_passing_saturation_report()
        readiness = engine.research_readiness(self.project)
        self.assertTrue(readiness["pass"], readiness["failures"])
        self.assertEqual(readiness["qualified_sources"], 120)
        with self.assertRaisesRegex(ValueError, "uvz-analysis"):
            engine.record_gate(self.project, "uvz", "approved", search_plan["artifact_id"])
        engine.record_gate(self.project, "uvz", "approved", uvz_v2["artifact_id"])

        engine.save_artifact(self.project, "ebook-charter", "# Charter\n\nA synthetic promise.")
        outline = engine.save_artifact(
            self.project, "ebook-outline", "# Outline\n\n1. Test the method", status="ready"
        )
        with self.assertRaisesRegex(ValueError, "ebook-outline"):
            engine.record_gate(self.project, "outline", "approved", search_plan["artifact_id"])
        engine.record_gate(self.project, "outline", "approved", outline["artifact_id"])
        engine.save_artifact(self.project, "claims-ledger", "# Claims\n\nC-001 [S0001]")
        engine.save_artifact(self.project, "terminology-ledger", "# Terms\n\nMethod: synthetic method")
        engine.save_artifact(self.project, "continuity-summary", "# Continuity\n\nNo open loops.")

        packet = engine.build_context_packet(
            self.project,
            "chapter",
            query="fictional method",
            chapter_number=1,
        )
        self.assertIn("Synthetic evidence", packet["packet"])
        self.assertFalse(packet["truncated"])

        chapter = (
            "# Chapter 1: Synthetic method\n\n"
            "A repeatable method can help the fictional reader complete the fictional task [S0001].\n\n"
            "## Action\n\nRun the synthetic checklist once and record the result."
        )
        engine.save_artifact(
            self.project,
            "chapter",
            chapter,
            status="ready",
            metadata={"chapter_number": 1, "chapter_title": "Synthetic method", "source_ids": ["S0001"]},
        )
        assembled = engine.assemble_ebook(self.project, title="Synthetic Ebook")
        self.assertTrue(Path(assembled["ebook_path"]).is_file())
        self.assertTrue(Path(assembled["sources_path"]).is_file())

        report = engine.quality_check(self.project)
        self.assertTrue(report["pass"], report["warnings"])
        final = engine.record_gate(self.project, "final", "approved", report["artifact"]["artifact_id"])
        self.assertEqual(final["status"], "approved")
        self.assertEqual(engine.project_status(self.project)["stage"], "final-approved")

    def test_uvz_gate_blocks_below_120_sources(self) -> None:
        source = self.project / "input" / "single-synthetic-source.txt"
        source.write_text("Synthetic source for minimum count enforcement.", encoding="utf-8")
        engine.ingest_file(self.project, source, title="Single synthetic source")
        self.save_passing_saturation_report()
        uvz = engine.save_artifact(self.project, "uvz-analysis", "# Synthetic UVZ", status="ready")
        readiness = engine.research_readiness(self.project)
        self.assertFalse(readiness["pass"])
        self.assertIn("qualified sources 1/120", readiness["failures"][0])
        with self.assertRaisesRegex(ValueError, "qualified sources 1/120"):
            engine.record_gate(self.project, "uvz", "approved", uvz["artifact_id"])

    def test_source_registration_deduplicates_urls(self) -> None:
        sources = [
            {
                "url": "https://example.invalid/synthetic#fragment",
                "title": "First",
                "source_type": "synthetic",
            },
            {
                "url": "https://example.invalid/synthetic",
                "title": "Duplicate",
                "source_type": "synthetic",
            },
        ]
        result = engine.register_sources(self.project, sources)
        self.assertFalse(result["sources"][0]["duplicate"])
        self.assertTrue(result["sources"][1]["duplicate"])
        self.assertEqual(result["sources"][0]["source_id"], result["sources"][1]["source_id"])

    def test_concurrent_artifact_writes_allocate_unique_versions(self) -> None:
        def save(index: int) -> dict[str, object]:
            return engine.save_artifact(self.project, "concurrency-probe", f"probe {index}")

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(save, range(12)))
        versions = sorted(cast(int, result["version"]) for result in results)
        self.assertEqual(versions, list(range(1, 13)))
        self.assertEqual(len({result["artifact_id"] for result in results}), 12)

    def test_private_network_urls_and_import_root_escape_are_blocked(self) -> None:
        with mock.patch.object(socket, "getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]) as dns, \
                self.assertRaisesRegex(ValueError, "non-public"):
            engine.validate_public_url("http://127.0.0.1/private")
        dns.assert_called_once_with("127.0.0.1", 80)

        outside = self.root / "outside.txt"
        outside.write_text("outside approved import roots", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "approved import roots"):
            engine.ingest_file(self.project, outside, title="Outside source")

    def test_context_packet_keeps_retrieved_evidence_when_artifacts_are_large(self) -> None:
        source = self.project / "input" / "context-source.txt"
        source.write_text("retrieval needle evidence that must survive packet budgeting", encoding="utf-8")
        engine.ingest_file(self.project, source, title="Context evidence")
        engine.save_artifact(self.project, "ebook-charter", "X" * 5000)
        engine.save_artifact(self.project, "ebook-outline", "# Outline")
        engine.save_artifact(self.project, "terminology-ledger", "# Terms")
        engine.save_artifact(self.project, "claims-ledger", "# Claims")
        engine.save_artifact(self.project, "continuity-summary", "# Continuity")
        packet = engine.build_context_packet(
            self.project, "chapter", query="retrieval needle", chapter_number=1, max_chars=2000
        )
        self.assertTrue(packet["truncated"])
        self.assertIn("## Retrieved evidence", packet["packet"])
        self.assertIn("retrieval needle", packet["packet"])
        self.assertIsNotNone(packet["retrieval_id"])

    def test_rejected_latest_chapter_is_not_assembled_and_zero_citation_fails(self) -> None:
        engine.save_artifact(
            self.project,
            "chapter",
            "# Chapter 1\n\nReady chapter with no citation.",
            status="ready",
            metadata={"chapter_number": 1, "source_ids": []},
        )
        engine.save_artifact(
            self.project,
            "chapter",
            "# Chapter 1\n\nREJECTED LATEST.",
            status="rejected",
            metadata={"chapter_number": 1, "source_ids": []},
        )
        assembled = engine.assemble_ebook(self.project, title="Safe assembly")
        manuscript = Path(assembled["ebook_path"]).read_text(encoding="utf-8")
        self.assertNotIn("REJECTED LATEST", manuscript)
        report = engine.quality_check(self.project)
        self.assertFalse(report["pass"])
        self.assertIn("Manuscript contains no source citations", report["warnings"])

    def test_mirrored_integrity_and_two_phase_purge_leave_one_ebook(self) -> None:
        primary_root = self.root / "primary"
        mirror_root = self.root / "mirror"
        status = engine.init_project(
            primary_root,
            "retention-project",
            "Synthetic retention topic",
            language="en",
            mirror_root=mirror_root,
        )
        project = Path(status["project"])
        for index in (1, 2):
            source = project / "input" / f"retention-{index}.txt"
            source.write_text(f"Unique retained evidence {index} for a safe ebook.", encoding="utf-8")
            engine.ingest_file(project, source, title=f"Retention evidence {index}")
        project_uuid = engine.read_project_metadata(project)["project_uuid"]
        mirror_project = mirror_root / "retention-project"
        engine.guarded_remove_project(project, project_uuid)
        restored = engine.restore_knowledge_base(mirror_project, primary_root)
        project = Path(restored["project"])
        self.assertTrue(restored["verification"]["pass"])
        saturation = {
            "format": "json",
            "coverage": {dimension: ["S0001"] for dimension in engine.RESEARCH_DIMENSIONS},
            "high_impact_claims": [{"claim_id": "C-001", "source_ids": ["S0001"]}],
            "recent_batches": [
                {"source_ids": ["S0001"], "new_material_claims": 0, "material_claims_before": 1, "uvz_changed": False, "promise_changed": False},
                {"source_ids": ["S0002"], "new_material_claims": 0, "material_claims_before": 1, "uvz_changed": False, "promise_changed": False},
            ],
        }
        engine.save_artifact(
            project, "saturation-report", json.dumps(saturation), status="ready", metadata=saturation
        )
        with (
            mock.patch.object(engine, "MIN_QUALIFIED_SOURCES", 2),
            mock.patch.object(engine, "MIN_SOURCES_PER_DIMENSION", 1),
            mock.patch.object(engine, "MIN_SOURCES_PER_HIGH_IMPACT_CLAIM", 1),
            mock.patch.object(engine, "SATURATION_BATCH_SIZE", 1),
        ):
            uvz = engine.save_artifact(project, "uvz-analysis", "# UVZ", status="ready")
            engine.record_gate(project, "uvz", "approved", uvz["artifact_id"])
            outline = engine.save_artifact(project, "ebook-outline", "# Outline", status="ready")
            engine.record_gate(project, "outline", "approved", outline["artifact_id"])
            engine.save_artifact(
                project,
                "chapter",
                "# Chapter 1\n\nVerified synthetic fact [S0001].",
                status="ready",
                metadata={"chapter_number": 1, "source_ids": ["S0001"]},
            )
            engine.assemble_ebook(project, title="Retained Ebook")
            report = engine.quality_check(project)
            self.assertTrue(report["pass"], report["warnings"])
            engine.record_gate(project, "final", "approved", report["artifact"]["artifact_id"])
        integrity = engine.verify_knowledge_base(project)
        self.assertTrue(integrity["pass"], integrity["errors"])
        self.assertTrue(integrity["mirror_verified"])
        release_dir = self.root / "release" / "retention-project"
        prepared = engine.prepare_ebook_retention(project, release_dir)
        with self.assertRaisesRegex(ValueError, "invalid"):
            engine.purge_knowledge_base(
                project,
                "wrong-token-that-is-long-enough",
                prepared["confirmation_phrase"],
            )
        with self.assertRaisesRegex(ValueError, "confirmation phrase"):
            engine.purge_knowledge_base(project, prepared["purge_token"], "PURGE WRONG PROJECT")
        purged = engine.purge_knowledge_base(
            project,
            prepared["purge_token"],
            prepared["confirmation_phrase"],
        )
        self.assertTrue(purged["primary_deleted"])
        self.assertTrue(purged["mirror_deleted"])
        self.assertFalse(project.exists())
        self.assertFalse((mirror_root / "retention-project").exists())
        self.assertEqual([item.name for item in release_dir.iterdir()], ["ebook.md"])
        self.assertIn("Retained Ebook", (release_dir / "ebook.md").read_text(encoding="utf-8"))

    def test_integrity_check_detects_and_repairs_mirror_tampering(self) -> None:
        primary_root = self.root / "tamper-primary"
        mirror_root = self.root / "tamper-mirror"
        status = engine.init_project(
            primary_root,
            "tamper-project",
            "Synthetic mirror tamper test",
            language="en",
            mirror_root=mirror_root,
        )
        project = Path(status["project"])
        source = project / "input" / "tamper.txt"
        source.write_text("Original evidence that should remain intact.", encoding="utf-8")
        ingested = engine.ingest_file(project, source, title="Tamper evidence")
        mirror_file = mirror_root / "tamper-project" / ingested["raw_path"]
        mirror_file.write_text("tampered", encoding="utf-8")
        broken = engine.verify_knowledge_base(project)
        self.assertFalse(broken["pass"])
        self.assertTrue(any("mirror hash mismatch" in item for item in broken["errors"]))
        engine.sync_durable_state(project, [ingested["raw_path"], ingested["text_path"]])
        repaired = engine.verify_knowledge_base(project)
        self.assertTrue(repaired["pass"], repaired["errors"])


class MCPProtocolTest(OfflineTestCase):
    def test_server_initializes_and_lists_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            messages = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "init_project",
                        "arguments": {
                            "root": temp_root,
                            "project_slug": "mcp-synthetic",
                            "topic": "Synthetic MCP protocol test only",
                            "language": "en",
                            "allow_single_copy": True,
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "search_corpus",
                        "arguments": {
                            "project_dir": str(Path(temp_root) / "mcp-synthetic"),
                            "query": "synthetic",
                            "limit": 999,
                        },
                    },
                },
            ]
            completed = subprocess.run(
                [sys.executable, "-B", "-m", "ebe", "mcp"],
                cwd=ROOT,
                timeout=30,
                input="\n".join(json.dumps(item) for item in messages) + "\n",
                text=True,
                capture_output=True,
                check=True,
            )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "EBE")
        self.assertIn("Offline EBE core", responses[0]["result"]["instructions"])
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "start_pipeline", "pipeline_status", "control_pipeline", "run_topic_pipeline",
                "plan_research", "submit_search_results", "research_status",
                "read_source_body", "review_source", "retry_research",
                "init_project",
                "project_status",
                "register_sources",
                "ingest_file",
                "search_corpus",
                "research_readiness",
                "save_artifact",
                "record_gate",
                "build_context_packet",
                "assemble_ebook",
                "quality_check",
                "verify_knowledge_base",
            },
        )
        self.assertFalse(responses[2]["result"]["isError"])
        self.assertEqual(responses[2]["result"]["structuredContent"]["stage"], "initialized")
        self.assertTrue(responses[3]["result"]["isError"])
        self.assertIn("maximum", responses[3]["result"]["content"][0]["text"])
        init_tool = next(
            tool for tool in responses[1]["result"]["tools"] if tool["name"] == "init_project"
        )
        self.assertEqual(init_tool["inputSchema"]["required"], ["root", "project_slug", "topic"])
        self.assertEqual(init_tool["inputSchema"]["properties"]["allow_single_copy"], {
            "type": "boolean", "default": False,
            "description": "Permit unsafe single-copy storage for disposable synthetic tests only."})

    def test_protocol_negotiation_and_parse_errors_do_not_leak_tracebacks(self) -> None:
        lines = [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2099-01-01",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
            ),
            '{"broken"',
        ]
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "ebe", "mcp"],
            cwd=ROOT,
            timeout=30,
            input="\n".join(lines) + "\n",
            text=True,
            capture_output=True,
            check=True,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(responses[1]["error"]["code"], -32600)
        self.assertEqual(responses[1]["error"]["message"], "invalid_request")
        self.assertNotIn("data", responses[1]["error"])
        self.assertNotIn("Traceback", completed.stdout)


if __name__ == "__main__":
    unittest.main()
