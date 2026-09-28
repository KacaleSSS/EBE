#!/usr/bin/env python3
"""Dependency-free MCP stdio server for the Topic to Ebook engine."""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Callable

import engine
import pipeline
import research


SERVER_INFO = {"name": "topic-to-ebook", "version": "0.5.0"}
SUPPORTED_PROTOCOL_VERSION = "2025-06-18"
SERVER_INSTRUCTIONS = (
    "Use a persistent project root. For live research, configure mirror_root on a separate durable "
    "mount. Call verify_knowledge_base after imports and before final release. Never call "
    "purge_knowledge_base without explicit user confirmation after prepare_ebook_retention."
)


def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


TOOLS: list[dict[str, Any]] = [
    {"name": "retry_research", "description": "Explicitly retry failed searches and acquisitions after correcting their cause; keeps completed work.",
     "inputSchema": schema({"project_dir": {"type": "string"}}, ["project_dir"])},
    {"name": "plan_research", "description": "Plan gap-directed queries or add model-expanded queries; query identities prevent accidental repeats.",
     "inputSchema": schema({"project_dir": {"type": "string"},
        "providers": {"type": "array", "items": {"enum": ["brave", "openalex", "codex"]}},
        "queries": {"type": "array", "items": schema({"query": {"type": "string"},
                    "dimension": {"enum": list(engine.RESEARCH_DIMENSIONS)},
                    "provider": {"enum": ["brave", "openalex", "codex"]}}, ["query", "dimension", "provider"])}}, ["project_dir"])},
    {"name": "run_research_batch", "description": "Search configured providers and acquire up to 20 bodies; return pending Codex searches and evidence review work.",
     "inputSchema": schema({"project_dir": {"type": "string"},
                            "batch_size": {"type": "integer", "minimum": 1, "maximum": 20},
                            "max_queries": {"type": "integer", "minimum": 0, "maximum": 5},
                            "max_attempts": {"type": "integer", "minimum": 1, "maximum": 5}}, ["project_dir"])},
    {"name": "submit_search_results", "description": "Record one page of Codex discovery results. Snippets never count as evidence.",
     "inputSchema": schema({"project_dir": {"type": "string"}, "query_id": {"type": "string"},
                            "page": {"type": "integer", "minimum": 0, "maximum": 9},
                            "has_more": {"type": "boolean"}, "candidates": {"type": "array", "items": {"type": "object"}}},
                           ["project_dir", "query_id", "page", "candidates"])},
    {"name": "research_status", "description": "Inspect search progress, retry timing, acquisition batches and evidence coverage gaps.",
     "inputSchema": schema({"project_dir": {"type": "string"}}, ["project_dir"])},
    {"name": "read_source_body", "description": "Read a bounded body window including sources awaiting evidence review; continue with next_offset.",
     "inputSchema": schema({"project_dir": {"type": "string"}, "source_id": {"type": "string"},
                            "offset": {"type": "integer", "minimum": 0},
                            "max_chars": {"type": "integer", "minimum": 100, "maximum": 30000}}, ["project_dir", "source_id"])},
    {"name": "review_source", "description": "Grade relevance and evidence role against an exact body excerpt. Only accepted bodies count toward 120.",
     "inputSchema": schema({"project_dir": {"type": "string"}, "source_id": {"type": "string"},
                            "relevant": {"type": "boolean"}, "evidence_role": {"enum": ["primary", "commercial", "case-study", "counterpoint", "secondary"]},
                            "quality_grade": {"enum": ["strong", "usable-with-limits", "weak"]},
                            "rationale": {"type": "string", "minLength": 1}, "supporting_excerpt": {"type": "string", "minLength": 30}},
                           ["project_dir", "source_id", "relevant", "evidence_role", "quality_grade", "rationale", "supporting_excerpt"])},
    {"name": "start_pipeline", "description": "Initialize a resumable topic pipeline; existing configuration is immutable.",
     "inputSchema": schema({"project_dir": {"type": "string"},
                            "chapter_count": {"type": "integer", "minimum": 1, "maximum": 30},
                            "auto_approve": {"type": "boolean"},
                            "commercial_goal": {"type": "string"},
                            "max_research_rounds": {"type": "integer", "minimum": 1, "maximum": 100},
                            "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10}}, ["project_dir"])},
    {"name": "pipeline_status", "description": "Read durable execution state and errors.",
     "inputSchema": schema({"project_dir": {"type": "string"}}, ["project_dir"])},
    {"name": "control_pipeline", "description": "Pause, resume, or explicitly reset exhausted retry attempts.",
     "inputSchema": schema({"project_dir": {"type": "string"}, "action": {"enum": ["pause", "resume", "retry"]}},
                           ["project_dir", "action"])},
    {"name": "run_topic_pipeline", "description": "Advance deterministic steps and return the next Codex work request. Submit its result and continue until complete or blocked.",
     "inputSchema": schema({"project_dir": {"type": "string"},
                            "max_steps": {"type": "integer", "minimum": 1, "maximum": 100},
                            "submission": schema({"task_id": {"type": "string"}, "ticket": {"type": "string"},
                                                   "result": schema({"content": {"type": "string", "minLength": 1},
                                                                     "metadata": {"type": "object"}}, ["content"])},
                                                  ["task_id", "ticket", "result"])}, ["project_dir"])},
    {
        "name": "init_project",
        "description": "Create a persistent topic research workspace and return its initial status.",
        "inputSchema": schema(
            {
                "root": {"type": "string", "description": "Parent directory for research projects."},
                "project_slug": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{1,62}$"},
                "topic": {"type": "string"},
                "language": {"type": "string", "default": "zh-CN"},
                "audience_hint": {"type": "string"},
                "mirror_root": {
                    "type": "string",
                    "description": "Independent durable path used as a verified project mirror.",
                },
                "import_roots": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Approved roots from which ingest_file may read.",
                },
                "allow_single_copy": {
                    "type": "boolean",
                    "default": False,
                    "description": "Permit unsafe single-copy storage for disposable synthetic tests only.",
                },
            },
            ["root", "project_slug", "topic"],
        ),
    },
    {
        "name": "project_status",
        "description": "Read the current stage, source counts, artifacts, and approval gates.",
        "inputSchema": schema({"project_dir": {"type": "string"}}, ["project_dir"]),
    },
    {
        "name": "register_sources",
        "description": "Register discovered source candidates before fetching them; duplicate URLs are reused.",
        "inputSchema": schema(
            {
                "project_dir": {"type": "string"},
                "sources": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "title": {"type": "string"},
                            "publisher": {"type": "string"},
                            "published_at": {"type": "string"},
                            "source_type": {"type": "string"},
                            "status": {"enum": ["candidate", "selected", "excluded"]},
                            "search_query": {"type": "string"},
                            "selected_reason": {"type": "string"},
                            "excluded_reason": {"type": "string"},
                            "metadata": {"type": "object"},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            ["project_dir", "sources"],
        ),
    },
    {
        "name": "ingest_url",
        "description": "Fetch a registered URL, save the raw file, extract text, and create searchable chunks.",
        "inputSchema": schema(
            {
                "project_dir": {"type": "string"},
                "source_id": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120, "default": 25},
                "max_bytes": {"type": "integer", "minimum": 1024, "default": engine.MAX_FETCH_BYTES},
            },
            ["project_dir", "source_id"],
        ),
    },
    {
        "name": "ingest_file",
        "description": "Copy a local source into the project, extract text, and create searchable chunks.",
        "inputSchema": schema(
            {
                "project_dir": {"type": "string"},
                "file_path": {"type": "string"},
                "title": {"type": "string"},
                "source_type": {"type": "string", "default": "user-supplied"},
                "max_bytes": {"type": "integer", "minimum": 1024, "default": engine.MAX_FETCH_BYTES},
            },
            ["project_dir", "file_path"],
        ),
    },
    {
        "name": "search_corpus",
        "description": "Retrieve the most relevant chunks from the ingested local corpus.",
        "inputSchema": schema(
            {
                "project_dir": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
            },
            ["project_dir", "query"],
        ),
    },
    {
        "name": "research_readiness",
        "description": "Check the 120 source floor, dimension coverage, claim support, and two batch saturation standard.",
        "inputSchema": schema({"project_dir": {"type": "string"}}, ["project_dir"]),
    },
    {
        "name": "save_artifact",
        "description": "Save a versioned research or writing artifact without overwriting earlier versions.",
        "inputSchema": schema(
            {
                "project_dir": {"type": "string"},
                "artifact_type": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{1,63}$"},
                "content": {"type": "string"},
                "status": {"enum": ["draft", "ready", "approved", "rejected", "superseded"], "default": "draft"},
                "metadata": {"type": "object"},
            },
            ["project_dir", "artifact_type", "content"],
        ),
    },
    {
        "name": "record_gate",
        "description": "Record a UVZ, outline, or final approval decision in the project history.",
        "inputSchema": schema(
            {
                "project_dir": {"type": "string"},
                "gate": {"enum": ["uvz", "outline", "final"]},
                "status": {"enum": ["approved", "rejected", "auto-approved"]},
                "artifact_id": {"type": "string"},
                "note": {"type": "string"},
            },
            ["project_dir", "gate", "status"],
        ),
    },
    {
        "name": "build_context_packet",
        "description": "Build a bounded stage-specific packet from saved artifacts and retrieved evidence.",
        "inputSchema": schema(
            {
                "project_dir": {"type": "string"},
                "stage": {"enum": ["research", "uvz", "charter", "chapter", "qa"]},
                "query": {"type": "string"},
                "chapter_number": {"type": "integer", "minimum": 1},
                "max_chars": {"type": "integer", "minimum": 2000, "maximum": 100000, "default": 30000},
            },
            ["project_dir", "stage"],
        ),
    },
    {
        "name": "assemble_ebook",
        "description": "Assemble the latest version of every numbered chapter into ebook.md and sources.md.",
        "inputSchema": schema(
            {"project_dir": {"type": "string"}, "title": {"type": "string"}},
            ["project_dir"],
        ),
    },
    {
        "name": "quality_check",
        "description": "Check gates, citations, unresolved markers, unreadable sources, and duplicate paragraphs.",
        "inputSchema": schema(
            {
                "project_dir": {"type": "string"},
                "duplicate_threshold": {"type": "number", "minimum": 0.5, "maximum": 1.0, "default": 0.82},
            },
            ["project_dir"],
        ),
    },
    {
        "name": "verify_knowledge_base",
        "description": "Verify SQLite integrity, source and artifact hashes, chunks, and the independent mirror.",
        "inputSchema": schema({"project_dir": {"type": "string"}}, ["project_dir"]),
    },
    {
        "name": "restore_knowledge_base",
        "description": "Restore a missing primary project from its verified independent mirror into an empty destination.",
        "inputSchema": schema(
            {
                "mirror_project_dir": {"type": "string"},
                "primary_root": {"type": "string"},
            },
            ["mirror_project_dir", "primary_root"],
        ),
    },
    {
        "name": "prepare_ebook_retention",
        "description": "Copy and verify one final ebook outside the project, then issue a short-lived purge token without deleting research data.",
        "inputSchema": schema(
            {
                "project_dir": {"type": "string"},
                "release_dir": {"type": "string"},
                "ttl_minutes": {"type": "integer", "minimum": 5, "maximum": 1440, "default": 60},
            },
            ["project_dir", "release_dir"],
        ),
    },
    {
        "name": "purge_knowledge_base",
        "description": "Permanently delete the verified primary and mirror knowledge bases while retaining exactly the prepared ebook.",
        "inputSchema": schema(
            {
                "project_dir": {"type": "string"},
                "purge_token": {"type": "string", "minLength": 20},
                "confirmation_phrase": {"type": "string", "minLength": 20},
            },
            ["project_dir", "purge_token", "confirmation_phrase"],
        ),
    },
]


TOOL_TITLES = {
    "retry_research": "Retry failed acquisition work",
    "plan_research": "Plan evidence searches", "run_research_batch": "Acquire research batch",
    "submit_search_results": "Record search results", "research_status": "Read research progress",
    "read_source_body": "Read source body", "review_source": "Review source evidence",
    "start_pipeline": "Start topic pipeline",
    "pipeline_status": "Read pipeline state",
    "control_pipeline": "Control pipeline execution",
    "run_topic_pipeline": "Advance topic pipeline",
    "init_project": "Initialize durable ebook project",
    "project_status": "Read ebook project status",
    "register_sources": "Register research sources",
    "ingest_url": "Ingest a public URL",
    "ingest_file": "Ingest an approved local file",
    "search_corpus": "Search the research knowledge base",
    "research_readiness": "Check research readiness",
    "save_artifact": "Save a versioned artifact",
    "record_gate": "Record an approval gate",
    "build_context_packet": "Build a bounded context packet",
    "assemble_ebook": "Assemble the ebook",
    "quality_check": "Run deterministic ebook QA",
    "verify_knowledge_base": "Verify knowledge base integrity",
    "restore_knowledge_base": "Restore knowledge base from mirror",
    "prepare_ebook_retention": "Prepare single ebook retention",
    "purge_knowledge_base": "Purge the research knowledge base",
}

MUTATING_TOOLS = {
    "retry_research",
    "plan_research", "run_research_batch", "submit_search_results", "research_status", "review_source",
    "start_pipeline", "pipeline_status", "control_pipeline", "run_topic_pipeline",
    "init_project",
    "register_sources",
    "ingest_url",
    "ingest_file",
    "search_corpus",
    "save_artifact",
    "record_gate",
    "assemble_ebook",
    "quality_check",
    "restore_knowledge_base",
    "prepare_ebook_retention",
    "purge_knowledge_base",
}

for tool in TOOLS:
    name = tool["name"]
    tool["title"] = TOOL_TITLES[name]
    tool["outputSchema"] = {"type": "object", "additionalProperties": True}
    tool["annotations"] = {
        "readOnlyHint": name not in MUTATING_TOOLS,
        "destructiveHint": name == "purge_knowledge_base",
        "openWorldHint": name in {"ingest_url", "run_research_batch"},
    }


def validate_value(value: Any, definition: dict[str, Any], path: str) -> None:
    if "enum" in definition and value not in definition["enum"]:
        raise ValueError(f"{path} must be one of {definition['enum']}")
    expected = definition.get("type")
    valid = True
    if expected == "object":
        valid = isinstance(value, dict)
    elif expected == "array":
        valid = isinstance(value, list)
    elif expected == "string":
        valid = isinstance(value, str)
    elif expected == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "number":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected == "boolean":
        valid = isinstance(value, bool)
    if expected and not valid:
        raise ValueError(f"{path} must be {expected}")
    if isinstance(value, str):
        if len(value) < definition.get("minLength", 0):
            raise ValueError(f"{path} is too short")
        if definition.get("pattern") and not re.fullmatch(definition["pattern"], value):
            raise ValueError(f"{path} does not match the required pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in definition and value < definition["minimum"]:
            raise ValueError(f"{path} is below the minimum")
        if "maximum" in definition and value > definition["maximum"]:
            raise ValueError(f"{path} exceeds the maximum")
    if isinstance(value, list) and definition.get("items"):
        for index, item in enumerate(value):
            validate_value(item, definition["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        properties = definition.get("properties", {})
        missing = [item for item in definition.get("required", []) if item not in value]
        if missing:
            raise ValueError(f"{path} is missing required fields: {', '.join(missing)}")
        if definition.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ValueError(f"{path} contains unsupported fields: {', '.join(extras)}")
        for key, item in value.items():
            if key in properties:
                validate_value(item, properties[key], f"{path}.{key}")


def call_tool(name: str, arguments: dict[str, Any]) -> Any:
    handlers: dict[str, Callable[..., Any]] = {
        "retry_research": research.retry_research,
        "plan_research": research.plan_research,
        "run_research_batch": research.run_research_batch,
        "submit_search_results": research.submit_search_results,
        "research_status": research.research_status,
        "read_source_body": research.read_source_body,
        "review_source": research.review_source,
        "start_pipeline": pipeline.start_pipeline,
        "pipeline_status": pipeline.pipeline_status,
        "control_pipeline": pipeline.control_pipeline,
        "run_topic_pipeline": pipeline.run_topic_pipeline,
        "init_project": engine.init_project,
        "project_status": engine.project_status,
        "register_sources": engine.register_sources,
        "ingest_url": engine.ingest_url,
        "ingest_file": engine.ingest_file,
        "search_corpus": engine.search_corpus,
        "research_readiness": engine.research_readiness,
        "save_artifact": engine.save_artifact,
        "record_gate": engine.record_gate,
        "build_context_packet": engine.build_context_packet,
        "assemble_ebook": engine.assemble_ebook,
        "quality_check": engine.quality_check,
        "verify_knowledge_base": engine.verify_knowledge_base,
        "restore_knowledge_base": engine.restore_knowledge_base,
        "prepare_ebook_retention": engine.prepare_ebook_retention,
        "purge_knowledge_base": engine.purge_knowledge_base,
    }
    if name not in handlers:
        raise ValueError(f"unknown tool: {name}")
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    definition = next(tool for tool in TOOLS if tool["name"] == name)
    validate_value(arguments, definition["inputSchema"], "arguments")
    return handlers[name](**arguments)


def success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def failure(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return success(
            request_id,
            {
                "protocolVersion": SUPPORTED_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": SERVER_INSTRUCTIONS,
            },
        )
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        return success(request_id, {})
    if method == "tools/list":
        return success(request_id, {"tools": TOOLS})
    if method == "tools/call":
        if request_id is None:
            return None
        try:
            value = call_tool(params.get("name", ""), params.get("arguments") or {})
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
            return success(
                request_id,
                {
                    "content": [{"type": "text", "text": rendered}],
                    "structuredContent": value,
                    "isError": False,
                },
            )
        except Exception as exc:
            rendered = json.dumps({"error": str(exc)}, ensure_ascii=False)
            return success(
                request_id,
                {
                    "content": [{"type": "text", "text": rendered}],
                    "isError": True,
                },
            )
    if request_id is None:
        return None
    return failure(request_id, -32601, f"method not found: {method}")


def main() -> int:
    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        request_id: Any = None
        try:
            message = json.loads(raw_line)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC message must be an object")
            request_id = message.get("id")
            response = handle(message)
        except json.JSONDecodeError as exc:
            response = failure(None, -32700, f"parse error at line {exc.lineno} column {exc.colno}")
        except Exception as exc:
            sys.stderr.write(f"topic-to-ebook MCP error: {type(exc).__name__}: {exc}\n")
            sys.stderr.flush()
            response = failure(request_id, -32603, "internal error")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
