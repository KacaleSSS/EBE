"""Offline sparse review planning, not an automatic fact verifier.

CLI contract: send each ``packets`` entry to a local reviewer. Return a dict
with claim_id, cache_key, status (support/conflict/insufficient), and citations
[{source_id, quote, stance}]. Quotes must be exact substrings of one excerpt.
Two independent eligible sources are required for structural support. Even a
valid support result has verified=False: quotation checks cannot prove truth.

Budget covers the sum of canonical JSON UTF-8 bytes of emitted packets, not
the plan metadata, caller prompts, or reviewer output. Bytes are a conservative
token-budget proxy, never a tokenizer measurement. Packets contain only excerpt
windows (at most 1500 UTF-8 bytes per source by default), never original bodies.
Offsets are Python Unicode character indexes, start inclusive/end exclusive.
Excerpt review is not full-document review; omitted text may change conclusions.
Risk groups are critical/key/high (all), medium/normal/low (ceil(20%)).
Optional claim group partitions sampling further. Unknown risks fail closed.
Records with status accepted or ingested and a body are eligible unless
accepted=False; optional accepted=True gives ingested records first priority.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

POLICY_VERSION = "ebe-sparse-v2-excerpts"
REVIEWER_MODEL_VERSION = "local-unspecified"
_HIGH = {"critical", "key", "high", "rapidly-changing"}
_LOW = {"medium", "normal", "low"}
_STATES = {"support", "conflict", "insufficient"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _string(value, label, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{label} must be a {'possibly empty ' if empty else 'nonempty '}string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    return value


def _norm(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _rows(value, label):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{label} must be a list of dicts")


def _records(records):
    _rows(records, "records")
    out = {}
    for row in records:
        sid = _string(row.get("source_id"), "source_id")
        if sid in out:
            raise ValueError("duplicate source_id")
        item = {"source_id": sid}
        for field in ("body", "url", "publisher", "doi", "family_id", "owner",
                      "quality_grade", "status", "text_sha256"):
            value = row.get(field, "")
            if value is None and field in {"url", "publisher", "doi", "family_id", "owner", "text_sha256"}:
                value = ""
            item[field] = _string(value, field, empty=True)
        digest = _hash(item["body"])
        supplied = item["text_sha256"]
        if supplied and (not re.fullmatch(r"[0-9a-fA-F]{64}", supplied) or supplied.lower() != digest):
            raise ValueError(f"text_sha256 does not match body: {sid}")
        item["text_sha256"] = digest
        accepted = row.get("accepted")
        if accepted is not None and type(accepted) is not bool:
            raise ValueError("accepted must be bool")
        item["accepted"] = accepted
        out[sid] = item
    return out


def _claims(claims):
    _rows(claims, "claims")
    out = {}
    for row in claims:
        cid = _string(row.get("id"), "claim id")
        if cid in out:
            raise ValueError("duplicate claim id")
        risk = _norm(_string(row.get("risk"), "risk"))
        if risk not in _HIGH | _LOW:
            raise ValueError(f"unknown risk: {risk}")
        ids = row.get("source_ids")
        if not isinstance(ids, list):
            raise ValueError("source_ids must be a list")
        ids = sorted({_string(sid, "source id") for sid in ids})
        out[cid] = {"id": cid, "text": _string(row.get("text"), "claim text"),
                    "risk": risk, "source_ids": ids,
                    "group": _string(row.get("group", risk), "group")}
    return out


def _families(records):
    # Inverted identity indexes + union-find, including non-eligible bridge rows.
    parent = {sid: sid for sid in records}

    def root(sid):
        while parent[sid] != sid:
            parent[sid] = parent[parent[sid]]
            sid = parent[sid]
        return sid

    seen = {}
    for sid in sorted(records):
        row = records[sid]
        keys = []
        for field in ("publisher", "owner", "family_id", "doi"):
            value = _norm(row[field])
            if field == "doi":
                value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
            if value:
                keys.append(("organization" if field in {"publisher", "owner"} else field, value))
        if not row["publisher"].strip() and not row["owner"].strip():
            keys.append(("organization", "<unknown>"))
        if row["body"].strip():
            keys.append(("body", row["text_sha256"]))
        if row["url"].strip():
            keys.append(("url", row["url"].strip()))
        for key in keys:
            if key in seen:
                a, b = root(sid), root(seen[key])
                parent[max(a, b)] = min(a, b)
            else:
                seen[key] = sid
    return {sid: root(sid) for sid in records}


def _eligible(row):
    return (bool(row["body"].strip()) and _norm(row["status"]) in {"accepted", "ingested"}
            and row["accepted"] is not False)


def _priority(row):
    accepted = row["accepted"] is True or _norm(row["status"]) == "accepted"
    return (not accepted, _norm(row["quality_grade"]) != "strong", row["source_id"])


def _keywords(text):
    # No language package: Latin/number words and overlapping CJK bigrams.
    terms = set(re.findall(r"[a-z0-9_]{2,}", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return sorted(terms, key=lambda t: (-len(t), t))[:24]


def _excerpt_source(row, text, limit):
    body = row["body"]
    total_bytes = len(body.encode("utf-8"))
    terms = _keywords(text)
    if total_bytes <= limit:
        windows = [{"start": 0, "end": len(body), "text": body}]
        method = "complete_short_body"
    else:
        # At most three non-overlapping contexts. Candidate count is bounded;
        # regex positions refer to original text, not a normalized copy.
        width = limit // 3
        anchors = []
        for term in terms:
            match = re.search(re.escape(term), body, flags=re.IGNORECASE)
            if match:
                anchors.append(match.start())
        method = "keyword_windows" if anchors else "leading_window_fallback"
        if not anchors:
            anchors = [next(i for i, char in enumerate(body) if not char.isspace())]
        candidates = {}
        for anchor in anchors:
            start = max(0, anchor - width // 8)
            fragment = body[start:start + width].encode("utf-8")[:width].decode("utf-8", errors="ignore")
            end = start + len(fragment)
            if fragment.strip():
                score = sum(term in fragment.casefold() for term in terms)
                candidates[(start, end)] = (score, fragment)
        windows = []
        for (start, end), (score, fragment) in sorted(candidates.items(), key=lambda item: (-item[1][0], item[0])):
            if not any(start < w["end"] and end > w["start"] for w in windows):
                windows.append({"start": start, "end": end, "text": fragment})
                if len(windows) == 3:
                    break
        windows.sort(key=lambda w: w["start"])
    source = {k: v for k, v in row.items() if k != "body"}
    source.update(excerpts=windows, original_body_sha256=row["text_sha256"],
                  original_body_chars=len(body), original_body_utf8_bytes=total_bytes,
                  offset_unit="unicode_codepoints", excerpt_method=method,
                  truncated=sum(w["end"] - w["start"] for w in windows) < len(body))
    return source


def _packet_records(sources, limit):
    """Validate excerpt structure separately from ingestion's full-body hashes."""
    _rows(sources, "sources")
    material = []
    for source in sources:
        if "body" in source:
            raise ValueError("packet sources must contain excerpts, not body")
        digest = source.get("original_body_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid original_body_sha256")
        if source.get("text_sha256") != digest:
            raise ValueError("original source hashes disagree")
        chars, byte_count = source.get("original_body_chars"), source.get("original_body_utf8_bytes")
        if type(chars) is not int or type(byte_count) is not int or not 0 < chars <= byte_count <= 4 * chars:
            raise ValueError("invalid original body length")
        if source.get("offset_unit") != "unicode_codepoints":
            raise ValueError("invalid offset unit")
        windows = source.get("excerpts")
        _rows(windows, "excerpts")
        if not 1 <= len(windows) <= 3:
            raise ValueError("invalid excerpt count")
        previous, covered, size = 0, 0, 0
        for window in windows:
            start, end = window.get("start"), window.get("end")
            text = _string(window.get("text"), "excerpt text")
            if (type(start) is not int or type(end) is not int or
                    not previous <= start < end <= chars or end - start != len(text)):
                raise ValueError("invalid excerpt offsets")
            previous = end
            covered += len(text)
            size += len(text.encode("utf-8"))
        if size > limit or size > byte_count:
            raise ValueError("excerpt byte limit exceeded")
        if type(source.get("truncated")) is not bool or source["truncated"] != (covered < chars):
            raise ValueError("invalid truncation marker")
        if not source["truncated"] and (size != byte_count or _hash("".join(w["text"] for w in windows)) != digest):
            raise ValueError("complete short body hash mismatch")
        # _records checks metadata and eligibility; do not compare excerpt hash
        # against the original hash. Restore original hash for independence.
        material.append(dict(source, body="\n".join(w["text"] for w in windows), text_sha256=""))
    rows = _records(material)
    for source in sources:
        rows[source["source_id"]]["text_sha256"] = source["original_body_sha256"]
    return rows


def build_review_plan(records, claims, budget=12000, *, policy_version=POLICY_VERSION,
                      reviewer_model_version=REVIEWER_MODEL_VERSION, excerpt_bytes=1500):
    """Return deterministic packets, per-claim unverified states and coverage/cost.

    Version kwargs bind cache entries to the actual downstream reviewer. Callers
    should replace local-unspecified before persisting/reusing review results.
    Cache context includes every record, so even a changed independence bridge
    invalidates results. Unknown source references are reported, never invented.
    """
    if type(budget) is not int or budget < 0:
        raise ValueError("budget must be a nonnegative integer")
    if type(excerpt_bytes) is not int or excerpt_bytes < 24:
        raise ValueError("excerpt_bytes must be an integer >= 24")
    _string(policy_version, "policy_version")
    _string(reviewer_model_version, "reviewer_model_version")
    records, claims = _records(records), _claims(claims)
    families = _families(records)
    context_hash = _hash(_json([{k: v for k, v in records[sid].items() if k != "body"}
                              for sid in sorted(records)]))
    selected = {cid for cid, c in claims.items() if c["risk"] in _HIGH}
    groups = {}
    for cid, claim in claims.items():
        if claim["risk"] in _LOW:
            groups.setdefault((claim["risk"], claim["group"]), []).append(cid)
    for ids in groups.values():
        ids.sort(key=lambda cid: (_hash(_json([cid, claims[cid]["text"]])), cid))
        selected.update(ids[:(len(ids) + 4) // 5])
    packets, states = [], []
    used = 0
    for cid in sorted(claims, key=lambda cid: (claims[cid]["risk"] not in _HIGH, cid)):
        claim = claims[cid]
        missing = [sid for sid in claim["source_ids"] if sid not in records]
        state = {"claim_id": cid, "verified": False, "status": "unverified",
                 "selected": cid in selected, "required": claim["risk"] in _HIGH,
                 "missing_source_ids": missing, "reason": "not_sampled"}
        states.append(state)
        if cid not in selected:
            continue
        candidates = sorted((records[sid] for sid in claim["source_ids"]
                             if sid in records and _eligible(records[sid])), key=_priority)
        sources, seen = [], set()
        for row in candidates:
            family = families[row["source_id"]]
            if family not in seen:
                seen.add(family)
                sources.append(dict(row, independence_id=family))
        state["independent_sources_available"] = len(sources)
        sources = [_excerpt_source(row, claim["text"], excerpt_bytes) for row in sources[:3]]
        if not sources:
            state["reason"] = "insufficient_evidence"
            continue
        packet = {"claim": claim, "sources": sources, "policy_version": policy_version,
                  "reviewer_model_version": reviewer_model_version, "context_hash": context_hash,
                  "minimum_independent_sources": 2, "excerpt_bytes_per_source": excerpt_bytes,
                  "review_scope": "excerpts_only", "full_document_review": False}
        while sources:
            packet["cache_key"] = _hash(_json({k: v for k, v in packet.items() if k != "cache_key"}))
            size = len(_json(packet).encode("utf-8"))
            if used + size <= budget:
                break
            sources.pop()
        if not sources:
            state["reason"] = "budget_exhausted"
            continue
        used += size
        packets.append(packet)
        state["reason"] = "pending_review" if len(sources) >= 2 else "insufficient_evidence"
        state["cache_key"] = packet["cache_key"]
    required = sum(s["required"] for s in states)
    required_packets = sum(p["claim"]["risk"] in _HIGH for p in packets)
    return {"packets": packets, "claims": states,
            "coverage": {"total_claims": len(claims), "selected_claims": len(selected),
                         "required_claims": required, "required_packet_claims": required_packets,
                         "required_deferred_claims": required - required_packets,
                         "packet_claims": len(packets), "verified_claims": 0,
                         "packet_fraction": len(packets) / len(claims) if claims else 0.0,
                         "sampled_claims": len(selected) - required},
            "cost": {"budget": budget, "utf8_bytes": used, "token_upper_bound": used,
                     "remaining": budget - used, "exact_tokens": None,
                     "scope": "canonical_packet_json_only", "model_calls": 0}}


def validate_review(packet, result):
    """Check binding, exact quotes and stance structure; never certify facts.

    Malformed inputs return machine_valid=False (not exceptions). Any conflict
    or insufficient stance escalates; support must cite two independent sources.
    Checks actual excerpt text, not only hashes. With no originals supplied this
    cannot authenticate excerpt provenance/offsets against full documents; use a
    trusted planner packet. Cache keys are integrity bindings, not signatures.
    """
    errors = []
    verdict = {"machine_valid": False, "verified": False, "status": "unverified",
               "review_status": "insufficient", "needs_escalation": True, "errors": errors,
               "review_scope": "excerpts_only", "full_document_review": False}
    try:
        if not isinstance(packet, dict) or not isinstance(result, dict):
            raise ValueError("packet and result must be dicts")
        claim = _claims([packet.get("claim")])
        cid = next(iter(claim))
        sources = packet.get("sources")
        limit = packet.get("excerpt_bytes_per_source")
        if type(limit) is not int or limit < 24:
            raise ValueError("invalid excerpt limit")
        if packet.get("review_scope") != "excerpts_only" or packet.get("full_document_review") is not False:
            raise ValueError("invalid review scope")
        rows = _packet_records(sources, limit)
        if not 1 <= len(rows) <= 3 or any(not _eligible(r) for r in rows.values()):
            raise ValueError("invalid packet sources")
        for field in ("policy_version", "reviewer_model_version", "context_hash", "cache_key"):
            _string(packet.get(field), field)
        if packet.get("minimum_independent_sources") != 2:
            raise ValueError("invalid independence threshold")
        expected = _hash(_json({k: v for k, v in packet.items() if k != "cache_key"}))
        if packet["cache_key"] != expected:
            raise ValueError("packet cache key mismatch")
        if result.get("claim_id") != cid or result.get("cache_key") != expected:
            raise ValueError("review binding mismatch")
        status = result.get("status")
        if not isinstance(status, str) or status not in _STATES:
            raise ValueError("invalid review status")
        citations = result.get("citations")
        _rows(citations, "citations")
        families = _families(rows)
        declared = {r["source_id"]: _string(r.get("independence_id"), "independence_id") for r in sources}
        excerpts = {r["source_id"]: r["excerpts"] for r in sources}
        supporting, supporting_local = set(), set()
        stances = set()
        for cite in citations:
            sid = _string(cite.get("source_id"), "citation source_id")
            quote = _string(cite.get("quote"), "quote")
            stance = cite.get("stance")
            if not isinstance(stance, str) or stance not in _STATES:
                raise ValueError("invalid citation stance")
            if (sid not in rows or sid not in claim[cid]["source_ids"] or
                    not any(quote in window["text"] for window in excerpts[sid])):
                raise ValueError("citation is not an exact excerpt fragment of a claim source")
            stances.add(stance)
            if stance == "support":
                supporting.add(declared[sid])
                supporting_local.add(families[sid])
        effective = ("conflict" if "conflict" in stances or status == "conflict" else
                     "insufficient" if "insufficient" in stances or status == "insufficient" else "support")
        if status == "conflict" and "conflict" not in stances:
            errors.append("conflict requires an exact conflict citation")
        if effective != status:
            errors.append("review status contradicts citation stances")
        if effective == "support" and min(len(supporting), len(supporting_local)) < 2:
            effective = "insufficient"
            errors.append("support requires two independent quoted sources")
        verdict.update(machine_valid=not errors, review_status=effective,
                       needs_escalation=bool(errors) or effective != "support")
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        errors.append(str(exc))
    return verdict
