"""Fresh cross-review and manuscript-bound final approval; no import-time hooks.

Local writers remain trusted. No model calls are made by publication gates.
An optional project.json cross_review_reviewer {model, endpoint} pins the
deployment's current reviewer. Without it the latest report names the reviewer;
unchanged model aliases cannot expose silently replaced weights.
"""
from contextlib import closing
from functools import wraps
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from ebe import evidence
from ebe.readiness import _object, _rows, _ids, _sources, required_reviews_readiness
from ebe.transfer import safe_child


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _project(conn):
    filename = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    if not filename:
        raise ValueError("publication requires a file-backed project database")
    return Path(filename).resolve().parent


def _latest(conn, kind):
    row = conn.execute("SELECT * FROM artifacts WHERE artifact_type=? ORDER BY version DESC LIMIT 1", (kind,)).fetchone()
    if row is None:
        raise ValueError(f"missing {kind} artifact")
    return dict(row)


def current_claims(conn):
    """The current saturation report, never the saved review plan, owns claims."""
    report = _object(_latest(conn, "saturation-report")["metadata_json"])
    high = report.get("high_impact_claims")
    if not isinstance(high, list) or not high:
        raise ValueError("saturation report requires complete high_impact_claims")
    ordinary = report.get("claims", [])
    if not isinstance(ordinary, list):
        raise ValueError("saturation report claims must be a list")
    claims, seen = [], set()
    for entry in high + ordinary:
        if not isinstance(entry, dict):
            raise ValueError("invalid saturation claim")
        cid = entry.get("claim_id", entry.get("id"))
        text = entry.get("text") or entry.get("claim")
        if not isinstance(cid, str) or not cid.strip() or cid in seen:
            raise ValueError("missing or duplicate saturation claim id")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{cid}: claim requires text or claim")
        seen.add(cid)
        claim = dict(id=cid, text=text, risk=entry.get("risk", "normal"),
                     source_ids=sorted(_ids(entry.get("source_ids"))))
        if "group" in entry:
            claim["group"] = entry["group"]
        claims.append(claim)
    return claims, {c.get("claim_id", c.get("id")) for c in high}


def _records(conn):
    # Same public record contract as review_service._load_records, on this exact
    # connection so final's write transaction observes one database snapshot.
    records = []
    project = _project(conn)
    for row in _rows(conn, "SELECT * FROM sources ORDER BY source_id"):
        meta = _object(row.get("metadata_json"))
        review = meta.get("evidence_review", {})
        if not isinstance(review, dict):
            raise ValueError("invalid evidence_review")
        body = safe_child(project, row["text_path"]).read_bytes().decode("utf-8") if row.get("text_path") else ""
        excerpt = review.get("excerpt")
        accepted = (row["status"] == "ingested" and review.get("relevant") is True
                    and review.get("grade") in {"strong", "usable", "usable-with-limits"}
                    and isinstance(excerpt, str) and len(excerpt.strip()) >= 30 and excerpt in body)
        records.append(dict(source_id=row["source_id"], body=body,
                            text_sha256=row.get("text_sha256") or "", url=row.get("url"),
                            publisher=row.get("publisher") or meta.get("publisher"),
                            doi=row.get("doi") or meta.get("doi"), owner=meta.get("owner"),
                            family_id=meta.get("family_id"), status=row["status"], accepted=accepted,
                            quality_grade=review.get("grade", "")))
    return records


def cross_review_from_connection(conn):
    """Rebuild rather than trusting saved required_readiness/pass/needs_escalation."""
    try:
        from ebe import review_service, local_model
        claims, high_ids = current_claims(conn)
        saved = _object(_latest(conn, "cross-review")["metadata_json"])
        binding = saved["reviewer"]
        if (not isinstance(binding, dict) or set(binding) != {"model", "endpoint", "service_version", "prompt_hash"}
                or any(not isinstance(v, str) or not v.strip() for v in binding.values())):
            raise ValueError("invalid reviewer binding")
        if (binding["service_version"] != review_service.SERVICE_VERSION or
                binding["prompt_hash"] != _digest([review_service._INSTRUCTIONS, local_model.SYSTEM_PROMPT])):
            raise ValueError("reviewer service or prompt version changed")
        project_config = _project(conn) / "project.json"
        if project_config.exists():
            expected = _object(project_config.read_text(encoding="utf-8")).get("cross_review_reviewer")
            if expected is not None and (not isinstance(expected, dict) or
                    expected != {k: binding[k] for k in ("model", "endpoint")}):
                raise ValueError("current reviewer model/endpoint changed")
        old_plan = saved["plan"]
        budget = old_plan["cost"]["budget"]
        fresh = evidence.build_review_plan(_records(conn), claims, budget,
                    policy_version=evidence.POLICY_VERSION, reviewer_model_version=_digest(binding))
        # Compare every state, packet and coverage count: omitted claims,
        # changed body hashes, sampling, prompts or policy invalidate the report.
        if fresh != old_plan:
            raise ValueError("cross-review plan stale or incomplete for current sources/claims/model/policy")
        checked_plan = dict(fresh, claims=[dict(s, required=s["required"] or s["claim_id"] in high_ids)
                                          for s in fresh["claims"]])
        result = required_reviews_readiness(checked_plan, saved["results"], evidence.validate_review)
        result["plan_digest"] = _digest(fresh)
        return result
    except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error) as exc:
        return {"pass": False, "failures": [f"cross-review invalid: {exc}"], "semantic_truth_verified": False}


def _artifact_text(project, row):
    raw = safe_child(project, row["file_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != row["sha256"]:
        raise ValueError(f"artifact content hash mismatch: {row['artifact_id']}")
    return raw.decode("utf-8")


def manuscript_binding(conn, engine):
    """Verify exact assembly against current chapter files and reference rows."""
    project = _project(conn)
    ebook = _latest(conn, "ebook")
    if ebook["status"] not in {"ready", "approved"}:
        raise ValueError("current ebook is not ready")
    manuscript = _artifact_text(project, ebook)
    if safe_child(project, "outputs/ebook.md").read_bytes() != manuscript.encode("utf-8"):
        raise ValueError("assembled ebook differs from latest ebook artifact")
    title, separator, _ = manuscript.partition("\n")
    if not separator or not title.startswith("# ") or not title[2:].strip():
        raise ValueError("assembled ebook has invalid title")
    latest = {}
    for row in _rows(conn, "SELECT * FROM artifacts WHERE artifact_type='chapter' ORDER BY version DESC"):
        meta = _object(row["metadata_json"])
        number = meta.get("chapter_number")
        if type(number) is not int or number < 1:
            raise ValueError("invalid chapter number")
        latest.setdefault(number, row)
    if not latest or sorted(latest) != list(range(1, max(latest) + 1)):
        raise ValueError("current chapters must be nonempty and contiguous")
    chapters = []
    content = []
    for number, row in sorted(latest.items()):
        if row["status"] not in {"ready", "approved"}:
            raise ValueError(f"latest chapter {number} is not ready")
        text = _artifact_text(project, row)
        if _ids(_object(row["metadata_json"]).get("source_ids")) != set(engine.CITATION_RE.findall(text)):
            raise ValueError(f"chapter {number} citation metadata mismatch")
        content.append(text.strip())
        chapters.append(row)
    body = title + "\n\n" + "\n\n".join(content) + "\n"
    cited = sorted(set(engine.CITATION_RE.findall(body)))
    if not cited:
        raise ValueError("current chapters require citations")
    all_sources = _sources(conn)
    references = ["# Sources", ""]
    for sid in cited:
        row = all_sources.get(sid)
        if not row or not row["eligible"]:
            raise ValueError(f"citation {sid} lacks current reviewed evidence")
        details = ", ".join(v for v in (row["publisher"], row["published_at"], row["source_type"]) if v)
        references.append(f"- [{sid}] {row['title']}. {details}. {row['url'] or 'local source'}")
    sources_text = "\n".join(references).rstrip() + "\n"
    if manuscript != body.rstrip() + "\n\n" + sources_text:
        raise ValueError("assembled ebook is stale relative to latest chapters/references")
    if safe_child(project, "outputs/sources.md").read_bytes() != sources_text.encode():
        raise ValueError("assembled reference output is stale")
    return {"schema": "ebe-final-v1", "ebook_sha256": ebook["sha256"], "ebook_artifact_id": ebook["artifact_id"],
            "chapters_digest": _digest(chapters), "sources_digest": _digest(all_sources),
            "saturation_digest": _digest(_latest(conn, "saturation-report")),
            "cross_review_digest": _digest(_latest(conn, "cross-review"))}


def install(engine):
    """Install only on engines exposing the production APIs (not import aliases)."""
    if not hasattr(engine, "quality_check") or not hasattr(engine, "record_gate"):
        return engine
    if getattr(engine.quality_check, "_ebe_bound_quality", False):
        return engine
    original_quality, original_gate = engine.quality_check, engine.record_gate

    @wraps(original_quality)
    def quality(project_dir, duplicate_threshold=0.82):
        project = engine.require_project(project_dir)
        with closing(engine.connect(project)) as conn:
            before = manuscript_binding(conn, engine)
        report = original_quality(project, duplicate_threshold=duplicate_threshold)
        with closing(engine.connect(project)) as conn:
            after = manuscript_binding(conn, engine)
            readiness = engine.research_readiness_from_connection(conn)
        if before != after:
            raise ValueError("publication inputs changed during quality_check")
        report["research_readiness"] = readiness
        report["pass"] = report.get("pass") is True and readiness["pass"] is True
        report["binding"] = after
        report.pop("artifact", None)
        artifact = engine.save_artifact(project, "quality-report", json.dumps(report, ensure_ascii=False),
                    status="ready", metadata={"pass": report["pass"], "ebook_sha256": after["ebook_sha256"],
                                               "binding": after})
        report["artifact"] = artifact
        return report

    @wraps(original_gate)
    def record(project_dir, gate, status, artifact_id=None, note=None):
        if gate != "final" or status not in {"approved", "auto-approved"}:
            return original_gate(project_dir, gate, status, artifact_id, note)
        project = engine.require_project(project_dir)
        with closing(engine.connect(project)) as conn:
            candidate = _latest(conn, "quality-report")
            before = manuscript_binding(conn, engine)
            if (candidate["artifact_id"] != artifact_id or candidate["status"] not in {"ready", "approved"}
                    or _object(candidate["metadata_json"]).get("binding") != before):
                raise ValueError("final requires latest current bound QA artifact; old/unbound QA refused")
            _artifact_text(project, candidate)
        fresh = quality(project)  # Actually execute QA, never trust caller-supplied pass.
        if fresh.get("pass") is not True:
            raise ValueError("final blocked by fresh quality_check")
        with closing(engine.connect(project)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = manuscript_binding(conn, engine)
            qa = _latest(conn, "quality-report")
            if current != before or current != fresh["binding"] or qa["artifact_id"] != fresh["artifact"]["artifact_id"]:
                raise ValueError("publication inputs or QA changed before final approval")
            _artifact_text(project, qa)
            qa_metadata = _object(qa["metadata_json"])
            if qa_metadata.get("binding") != current or qa_metadata.get("pass") is not True:
                raise ValueError("fresh QA artifact changed before final approval")
            ready = engine.research_readiness_from_connection(conn)
            if not ready["pass"]:
                raise ValueError("final research readiness failed: " + "; ".join(ready["failures"]))
            for prior_gate in ("uvz", "outline"):
                prior = engine.latest_gate(conn, prior_gate)
                if not prior or prior["status"] not in {"approved", "auto-approved"}:
                    raise ValueError(f"final requires approved {prior_gate} gate")
            # Repeat disk hashes immediately before recording; SQLite serializes
            # database writers, while arbitrary external filesystem writers remain trusted.
            if manuscript_binding(conn, engine) != current:
                raise ValueError("publication files changed before final approval")
            decision_id = "D-final-" + uuid4().hex
            approved_id = qa["artifact_id"]
            conn.execute("INSERT INTO decisions(decision_id,gate,status,artifact_id,note,created_at) VALUES (?,?,?,?,?,?)",
                         (decision_id, gate, status, approved_id, note, engine.now_iso()))
            engine.log_event(conn, "gate_recorded", {"gate": gate, "status": status, "artifact_id": approved_id})
            conn.commit()
        return {"decision_id": decision_id, "gate": gate, "status": status, "artifact_id": approved_id,
                "requested_artifact_id": artifact_id, "durability": engine.sync_durable_state(project)}

    quality._ebe_bound_quality = True
    engine.quality_check, engine.record_gate = quality, record
    return engine
