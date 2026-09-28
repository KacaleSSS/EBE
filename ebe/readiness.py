"""Fail-closed publication checks layered over the retained engine.

No import-time patching: EBE's loader must call install(engine), once per engine.
Saved cross-reviews are revalidated against current sources and claims.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from urllib.parse import urlsplit


def _norm(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split()) if isinstance(value, str) else ""


def _object(value):
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("metadata must be an object")
    return parsed


def _ids(value):
    if not isinstance(value, list) or any(not isinstance(s, str) or not s for s in value):
        raise ValueError("source_ids must be a list of nonempty strings")
    if len(set(value)) != len(value):
        raise ValueError("duplicate source_ids")
    return set(value)


def _rows(conn, sql):
    cursor = conn.execute(sql)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _sources(conn):
    database = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
    project = Path(database).resolve().parent if database else None
    sources = {}
    for row in _rows(conn, "SELECT * FROM sources"):
        metadata = _object(row.get("metadata_json"))
        body = ""
        if row.get("text_path") and project:
            path = (project / row["text_path"]).resolve()
            if path.is_relative_to(project):
                try:
                    body = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    pass
        review = metadata.get("evidence_review")
        review = review if isinstance(review, dict) else {}
        excerpt = review.get("excerpt")
        row.update(metadata=metadata, body=body, review=review,
                   body_hash=hashlib.sha256(_norm(body).encode()).hexdigest(),
                   eligible=(row.get("status") == "ingested" and bool(body.strip())
                             and review.get("relevant") is True
                             and review.get("grade") in ("strong", "usable", "usable-with-limits")
                             and isinstance(excerpt, str) and len(excerpt.strip()) >= 30
                             and excerpt in body))
        sources[row["source_id"]] = row
    return sources


def provenance_groups(sources):
    """Transitive union, including unqualified bridge rows; unknowns coalesce.

    Hostnames also merge subdomains conservatively. A small two-label suffix
    heuristic intentionally over-merges unknown suffixes rather than asserting
    independence. It is not a public-suffix ownership verification service.
    """
    parent = {sid: sid for sid in sources}

    def root(sid):
        while parent[sid] != sid:
            parent[sid] = parent[parent[sid]]
            sid = parent[sid]
        return sid

    seen = {}
    for sid, row in sources.items():
        meta = row["metadata"]
        keys = []
        for field in ("owner", "publisher"):
            for value in (row.get(field), meta.get(field)):
                if _norm(value):
                    keys.append(("organization", _norm(value)))
        url = row.get("url") or ""
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if host:
            labels = host.split(".")
            width = 3 if len(labels) > 2 and labels[-2] in {"co", "com", "org", "net", "gov", "ac", "edu"} else 2
            keys.append(("host", ".".join(labels[-width:])))
        if not keys:
            keys.append(("organization", "<unknown>"))
        for value in (row.get("doi"), meta.get("doi")):
            doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", _norm(value))
            if doi:
                keys.append(("doi", doi))
        if row["body"].strip():
            keys.append(("body", row["body_hash"]))
        for key in keys:
            if key in seen:
                a, b = root(sid), root(seen[key])
                parent[max(a, b)] = min(a, b)
            else:
                seen[key] = sid
    return {sid: root(sid) for sid in sources}


def _current_primary(row, risk, today):
    if row["review"].get("role") != "primary":
        return False
    value = row.get("published_at")
    try:
        published = date.fromisoformat(value)
    except (ValueError, TypeError):
        return False
    return 0 <= (today - published).days <= (90 if risk == "rapidly-changing" else 365)


def strict_readiness_from_connection(conn, engine, *, today=None):
    """Read-only strict gate. No missing metadata defaults to saturation."""
    today = today or datetime.now(timezone.utc).date()
    sources = _sources(conn)
    groups = provenance_groups(sources)
    eligible = {sid: row for sid, row in sources.items() if row["eligible"]}
    failures = []
    unique = len({row["body_hash"] for row in eligible.values()})
    if unique < 120:
        failures.append(f"strict qualified content {unique}/120; evidence_review and matching excerpt required")
    reports = _rows(conn, "SELECT * FROM artifacts WHERE artifact_type='saturation-report' ORDER BY version DESC LIMIT 1")
    meta = _object(reports[0].get("metadata_json")) if reports else {}

    def independent(value, label):
        ids = _ids(value)
        invalid = ids - eligible.keys()
        if invalid:
            failures.append(f"{label}: unreviewed or invalid sources {sorted(invalid)}")
        return ids & eligible.keys(), len({groups[sid] for sid in ids if sid in eligible})

    coverage = meta.get("coverage", {})
    if not isinstance(coverage, dict):
        raise ValueError("coverage must be an object")
    counts = {}
    for dimension in engine.RESEARCH_DIMENSIONS:
        _, count = independent(coverage.get(dimension, []), dimension)
        counts[dimension] = count
        if count < 3:
            failures.append(f"{dimension}: {count}/3 independent provenance groups")
    claims = meta.get("high_impact_claims")
    if not isinstance(claims, list) or not claims:
        failures.append("nonempty high_impact_claims required")
        claims = []
    claim_ids = set()
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str) or not claim["claim_id"].strip():
            raise ValueError("invalid high impact claim")
        cid = claim["claim_id"]
        if cid in claim_ids:
            failures.append(f"duplicate claim_id: {cid}")
        claim_ids.add(cid)
        ids, count = independent(claim.get("source_ids", []), cid)
        if count < 2:
            failures.append(f"{cid}: {count}/2 independent provenance groups")
        risk = claim.get("risk", "normal")
        if risk not in ("normal", "low", "medium", "high", "critical", "key", "rapidly-changing"):
            failures.append(f"{cid}: unknown risk")
        if risk in ("high", "critical", "key", "rapidly-changing"):
            authorities = _ids(claim.get("authoritative_source_ids", [])) & ids
            if not any(_current_primary(eligible[sid], risk, today) for sid in authorities):
                failures.append(f"{cid}: reviewed primary current published_at required")

    # Research creates this one table lazily. An initialized project with no
    # acquisitions has zero batches; readiness must not migrate its database.
    batch_table = conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE name='acquisition_batches'"
    ).fetchone()
    actual = (_rows(conn, "SELECT batch_id, source_ids FROM main.acquisition_batches ORDER BY batch_id DESC LIMIT 2")
              if batch_table else [])
    batches = meta.get("recent_batches")
    if len(actual) != 2 or not isinstance(batches, list) or len(batches) < 2:
        failures.append("two real recent acquisition batches required")
    else:
        expected = {row["batch_id"]: _ids(json.loads(row["source_ids"])) for row in actual}
        matched = set()
        content_sets = []
        for batch in batches[-2:]:
            if not isinstance(batch, dict):
                raise ValueError("invalid batch")
            bid = batch.get("batch_id")
            if type(bid) is not int or bid not in expected or bid in matched:
                failures.append("batch_id must identify each of the latest two real batches exactly once")
                continue
            matched.add(bid)
            ids, _ = independent(batch.get("source_ids", []), f"batch {bid}")
            if _ids(batch.get("source_ids", [])) != expected[bid]:
                failures.append(f"batch {bid}: source_ids do not match acquisition_batches")
            hashes = {eligible[sid]["body_hash"] for sid in ids}
            content_sets.append(hashes)
            if len(hashes) < max(20, engine.SATURATION_BATCH_SIZE):
                failures.append(f"batch {bid}: insufficient unique reviewed content")
            fields = ("new_material_claims", "material_claims_before", "uvz_changed", "promise_changed")
            if any(type(batch.get(key)) is not int or batch[key] < 0 for key in fields):
                failures.append(f"batch {bid}: explicit non-bool nonnegative integer counts and changed fields required")
                continue
            if batch["material_claims_before"] <= 0:
                failures.append(f"batch {bid}: material_claims_before must be positive")
            elif batch["new_material_claims"] / batch["material_claims_before"] > min(0.05, engine.MAX_NEW_CLAIM_RATE):
                failures.append(f"batch {bid}: new material claim rate exceeds threshold")
            if batch["uvz_changed"] != 0 or batch["promise_changed"] != 0:
                failures.append(f"batch {bid}: boundaries changed")
        if len(content_sets) == 2 and content_sets[0] & content_sets[1]:
            failures.append("recent batches repeat normalized content")
    return {"pass": not failures, "failures": failures, "qualified_sources": unique,
            "coverage": counts, "provenance_groups": groups, "semantic_truth_verified": False}


def required_reviews_readiness(plan, results, validator):
    """Separate adapter: every required claim must have a bound support result.

    validator is evidence.validate_review; no evidence module import or stored
    filename is required. The caller must supply a freshly built current plan.
    """
    failures = []
    try:
        states = plan["claims"]
        packets = {p["claim"]["id"]: p for p in plan["packets"]}
        indexed = {}
        for result in results:
            cid = result["claim_id"]
            if cid in indexed:
                raise ValueError("duplicate review result")
            indexed[cid] = result
        seen = set()
        for state in states:
            cid = state["claim_id"]
            if cid in seen or type(state.get("required")) is not bool:
                raise ValueError("invalid claim states")
            seen.add(cid)
            if not state["required"] and state.get("selected") is not True:
                continue
            packet, result = packets.get(cid), indexed.get(cid)
            if packet is None or result is None or state.get("cache_key") != packet.get("cache_key"):
                failures.append(f"{cid}: required review missing or stale")
                continue
            verdict = validator(packet, result)
            if (verdict.get("machine_valid") is not True or verdict.get("review_status") != "support"
                    or verdict.get("needs_escalation") is not False):
                failures.append(f"{cid}: required review not supported")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        failures.append(f"invalid review payload: {exc}")
    return {"pass": not failures, "failures": failures, "semantic_truth_verified": False}


def install(engine):
    """Idempotently wrap this engine instance; never patch legacy on import."""
    original = engine.research_readiness_from_connection
    if getattr(original, "_ebe_strict_readiness", False):
        from ebe.gates import install as install_gates
        install_gates(engine)
        return engine

    @wraps(original)
    def checked(connection):
        try:
            legacy = original(connection)
        except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as exc:
            legacy = {"pass": False, "failures": [f"legacy readiness invalid: {exc}"]}
        try:
            strict = strict_readiness_from_connection(connection, engine)
            from ebe.gates import cross_review_from_connection
            review = cross_review_from_connection(connection)
            strict["cross_review"] = review
            strict["failures"].extend(review["failures"])
            strict["pass"] = strict["pass"] and review["pass"]
        except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error) as exc:
            strict = {"pass": False, "failures": [f"strict readiness invalid: {exc}"]}
        result = dict(legacy)
        result.update(legacy_readiness=legacy, strict_readiness=strict,
                      failures=list(legacy.get("failures", [])) + strict["failures"])
        result["pass"] = legacy.get("pass") is True and strict["pass"] is True
        return result

    checked._ebe_strict_readiness = True
    engine.research_readiness_from_connection = checked
    from ebe.gates import install as install_gates
    install_gates(engine)
    return engine
