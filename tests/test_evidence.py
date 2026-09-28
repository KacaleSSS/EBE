"""Standard-library acceptance tests: python -B -m unittest discover -s tests."""
import copy
import hashlib
import json
import unittest

from ebe.evidence import build_review_plan, validate_review


def record(sid, **kw):
    row = dict(source_id=sid, body=f"Evidence from {sid}.", url=f"https://{sid}.example/",
               publisher=sid, doi=None, quality_grade="strong", status="ingested")
    row.update(kw)
    return row


def claim(cid="c", risk="high", ids=None, **kw):
    return dict(id=cid, text=f"Claim {cid}", risk=risk,
                source_ids=ids if ids is not None else ["a", "b", "c"], **kw)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.rows = [record(x) for x in "abc"]

    def plan(self, **kw):
        return build_review_plan(self.rows, [claim()], **kw)

    def packet(self):
        return self.plan()["packets"][0]

    def result(self, packet, status="support"):
        return dict(claim_id=packet["claim"]["id"], cache_key=packet["cache_key"], status=status,
                    citations=[dict(source_id=r["source_id"], quote=r["excerpts"][0]["text"], stance=status)
                               for r in packet["sources"]])

    def test_cap_and_preference(self):
        self.rows += [record("d", accepted=True), record("e", quality_grade="weak")]
        p = build_review_plan(self.rows, [claim(ids=list("abcde"))])["packets"][0]
        self.assertEqual(len(p["sources"]), 3)
        self.assertEqual(p["sources"][0]["source_id"], "d")

    def test_transitive_bridge_ineligible(self):
        self.rows = [record("a", publisher=" A Corp "), record("b", publisher="a corp", doi="10.1/x", status="rejected"),
                     record("c", doi="https://doi.org/10.1/X")]
        p = self.plan()
        self.assertEqual(len(p["packets"][0]["sources"]), 1)
        self.assertEqual(p["claims"][0]["reason"], "insufficient_evidence")

    def test_owner_family_hash(self):
        for override in ({"owner": "parent"}, {"family_id": "f"}, {"body": "same body"},
                         {"publisher": ""}, {"doi": "10.1/same"}, {"url": "https://same.example"}):
            with self.subTest(override=override):
                p = build_review_plan([record(x, **override) for x in "abc"], [claim()])
                self.assertEqual(len(p["packets"][0]["sources"]), 1)

    def test_unknown_and_rejected(self):
        self.rows = [record("a", accepted=False), record("b", status="pending"), record("c", body=" ")]
        p = build_review_plan(self.rows, [claim(ids=["a", "b", "c", "missing"])])
        self.assertEqual(p["packets"], [])
        self.assertEqual(p["claims"][0]["missing_source_ids"], ["missing"])

    def test_sampling_exact_ceiling_per_group_order_invariant(self):
        claims = [claim(f"{r}{i}", r) for r in ("high", "low", "medium") for i in range(11)]
        p = build_review_plan(self.rows, claims, 100000)
        q = build_review_plan(self.rows[::-1], claims[::-1], 100000)
        self.assertEqual(p, q)
        self.assertEqual(p["coverage"]["required_claims"], 11)
        self.assertEqual(p["coverage"]["sampled_claims"], 6)
        self.assertTrue(all(not c["verified"] for c in p["claims"]))

    def test_singleton_groups_and_key(self):
        p = build_review_plan(self.rows, [claim("a", "low", group="one"),
                                        claim("b", "low", group="two"), claim("c", "key")])
        self.assertEqual(p["coverage"]["selected_claims"], 3)

    def test_utf8_hard_budget(self):
        self.rows[0]["body"] = "中文🙂" * 20
        p = self.plan()
        size = sum(len(json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                   for x in p["packets"])
        self.assertEqual(p["cost"]["utf8_bytes"], size)
        self.assertEqual(len(self.plan(budget=size)["packets"][0]["sources"]), 3)
        for budget in (0, 1, size - 1, size):
            q = self.plan(budget=budget)
            self.assertLessEqual(q["cost"]["utf8_bytes"], budget)
            self.assertIsNone(q["cost"]["exact_tokens"])
        self.assertEqual(self.plan(budget=0)["coverage"]["required_deferred_claims"], 1)

    def test_high_priority_under_budget(self):
        claims = [claim("a", "low"), claim("z", "high")]
        p = build_review_plan(self.rows, claims, self.plan()["cost"]["utf8_bytes"])
        self.assertEqual(p["packets"][0]["claim"]["id"], "z")

    def test_cache_invalidation(self):
        key = self.packet()["cache_key"]
        for field, value in (("body", "new text"), ("publisher", "new org"),
                             ("quality_grade", "weak"), ("status", "rejected")):
            rows = copy.deepcopy(self.rows)
            rows[0][field] = value
            self.assertNotEqual(build_review_plan(rows, [claim()])["packets"][0]["cache_key"], key)
        for kw in ({"policy_version": "v2"}, {"reviewer_model_version": "model@2"}):
            self.assertNotEqual(self.plan(**kw)["packets"][0]["cache_key"], key)
        for field in ("id", "text", "risk"):
            c = claim()
            c[field] = "critical" if field == "risk" else "changed"
            self.assertNotEqual(build_review_plan(self.rows, [c])["packets"][0]["cache_key"], key)

    def test_hash_integrity(self):
        self.rows[0]["text_sha256"] = hashlib.sha256(self.rows[0]["body"].encode()).hexdigest().upper()
        self.plan()
        self.rows[0]["body"] += "changed"
        with self.assertRaises(ValueError):
            self.plan()

    def test_no_input_mutation(self):
        rows, claims = copy.deepcopy(self.rows), [claim()]
        before = copy.deepcopy((rows, claims))
        build_review_plan(rows, claims, 1000)
        self.assertEqual((rows, claims), before)

    def test_sparse_large_input_and_bridge_cache_change(self):
        claims = [claim(str(i), "low") for i in range(2000)]
        p = build_review_plan(self.rows, claims, 0)
        self.assertEqual(p["coverage"]["sampled_claims"], 400)
        self.assertEqual(p["coverage"]["verified_claims"], 0)
        original = self.packet()["cache_key"]
        self.rows.append(record("bridge", publisher="a", owner="b", status="rejected"))
        packet = self.packet()
        self.assertNotEqual(packet["cache_key"], original)
        self.assertEqual(len(packet["sources"]), 2)

    def test_valid_support_never_verifies(self):
        p = self.packet()
        v = validate_review(p, self.result(p))
        self.assertTrue(v["machine_valid"])
        self.assertFalse(v["verified"])
        self.assertEqual(v["status"], "unverified")

    def test_conflict_and_insufficient_escalate(self):
        p = self.packet()
        for status in ("conflict", "insufficient"):
            r = self.result(p, status)
            v = validate_review(p, r)
            self.assertTrue(v["machine_valid"])
            self.assertTrue(v["needs_escalation"])
            self.assertFalse(v["verified"])
        r = self.result(p, "insufficient")
        r["citations"] = []
        self.assertTrue(validate_review(p, r)["machine_valid"])

    def test_conflicting_stance_cannot_hide_under_support(self):
        p = self.packet()
        r = self.result(p)
        r["citations"][0]["stance"] = "conflict"
        v = validate_review(p, r)
        self.assertFalse(v["machine_valid"])
        self.assertEqual(v["review_status"], "conflict")

    def test_fake_quotes_and_unknown_sources(self):
        p = self.packet()
        for field, value in (("quote", "Evidence from".upper()), ("quote", ""),
                             ("quote", " "), ("source_id", "missing"), ("stance", [])):
            r = self.result(p)
            r["citations"][0][field] = value
            self.assertFalse(validate_review(p, r)["machine_valid"])

    def test_repeated_citations_do_not_count(self):
        p = self.packet()
        r = self.result(p)
        r["citations"] = [r["citations"][0]] * 3
        self.assertFalse(validate_review(p, r)["machine_valid"])

    def test_binding_and_tamper(self):
        p = self.packet()
        r = self.result(p)
        for field in ("claim_id", "cache_key"):
            bad = dict(r, **{field: "stale"})
            self.assertFalse(validate_review(p, bad)["machine_valid"])
        p["sources"][0]["body"] = "tampered"
        self.assertFalse(validate_review(p, r)["machine_valid"])

    def test_empty(self):
        p = build_review_plan([], [], 0)
        self.assertEqual(p["coverage"]["packet_fraction"], 0)
        self.assertEqual(p["cost"]["utf8_bytes"], 0)

    def test_bad_types(self):
        for value in (True, -1, 1.5, "100", None):
            with self.subTest(budget=value), self.assertRaises(ValueError):
                self.plan(budget=value)
        for rows, claims in (({}, []), ([], {}), ([None], []), ([], [None]),
                             (self.rows * 2, []), ([], [claim(), claim()]),
                             ([], [claim(risk="mystery")]), ([], [dict(claim(), source_ids="a")]),
                             ([record("a", body=12)], []), ([record("a", accepted=1)], []),
                             ([], [dict(claim(), text="\ud800")])):
            with self.subTest(rows=rows, claims=claims), self.assertRaises(ValueError):
                build_review_plan(rows, claims)

    def test_malformed_results_fail_closed(self):
        p = self.packet()
        for r in (None, [], {}, dict(self.result(p), status=[]), dict(self.result(p), citations=None)):
            self.assertFalse(validate_review(p, r)["machine_valid"])
        for bad in (None, [], {}, dict(p, sources=None)):
            self.assertFalse(validate_review(bad, self.result(p))["machine_valid"])

    def test_long_documents_fit_and_preserve_offsets(self):
        rows = [record(str(i), body="背景" * 25000 + "量子电池储能效率显著提高。" + "材料" * 25000 + str(i))
                for i in range(120)]
        c = dict(claim(ids=[str(i) for i in range(120)]), text="量子电池储能效率提高")
        for risk in ("key", "high"):
            c["risk"] = risk
            plan = build_review_plan(rows, [c])
            self.assertEqual(plan["coverage"]["required_packet_claims"], 1)
            self.assertLessEqual(plan["cost"]["utf8_bytes"], 12000)
            p = plan["packets"][0]
            self.assertEqual(len(p["sources"]), 3)
            self.assertFalse(p["full_document_review"])
            originals = {r["source_id"]: r["body"] for r in rows}
            for source in p["sources"]:
                body = originals[source["source_id"]]
                self.assertNotIn("body", source)
                self.assertTrue(source["truncated"])
                self.assertEqual(source["original_body_sha256"], hashlib.sha256(body.encode()).hexdigest())
                self.assertLessEqual(sum(len(w["text"].encode()) for w in source["excerpts"]), 1500)
                self.assertTrue(any("量子电池" in w["text"] for w in source["excerpts"]))
                for w in source["excerpts"]:
                    self.assertEqual(w["text"], body[w["start"]:w["end"]])
            v = validate_review(p, self.result(p))
            self.assertTrue(v["machine_valid"], v)
            self.assertFalse(v["full_document_review"])
            self.assertFalse(v["verified"])
            self.assertEqual(plan, build_review_plan(rows[::-1], [c]))

    def test_omitted_text_changes_cache_and_supplied_hash_checked(self):
        rows = [record(x, body="Context " * 15000 + x) for x in "abc"]
        p = build_review_plan(rows, [claim()])["packets"][0]
        rows[0]["body"] += " omitted revision"
        q = build_review_plan(rows, [claim()])["packets"][0]
        self.assertEqual(p["sources"][0]["excerpts"], q["sources"][0]["excerpts"])
        self.assertNotEqual(p["cache_key"], q["cache_key"])
        self.assertFalse(validate_review(q, self.result(p))["machine_valid"])
        rows[0]["text_sha256"] = p["sources"][0]["original_body_sha256"]
        with self.assertRaises(ValueError):
            build_review_plan(rows, [claim()])

    def test_quote_must_be_in_excerpt_even_when_hashes_match(self):
        rows = [record(x, body="prefix " * 15000 + "OMITTED_UNIQUE_QUOTE" + x) for x in "abc"]
        p = build_review_plan(rows, [claim()])["packets"][0]
        r = self.result(p)
        r["citations"][0]["quote"] = "OMITTED_UNIQUE_QUOTE"
        self.assertFalse(validate_review(p, r)["machine_valid"])

    def test_disjoint_windows_no_cross_window_quote(self):
        body = "α" * 10000 + "alpha target" + "β" * 10000 + "omega target" + "γ" * 10000
        p = build_review_plan([record(x, body=body + x) for x in "abc"],
                              [dict(claim(), text="alpha omega")])["packets"][0]
        windows = p["sources"][0]["excerpts"]
        self.assertEqual(len(windows), 2)
        r = self.result(p)
        r["citations"][0]["quote"] = windows[0]["text"][-10:] + "\n" + windows[1]["text"][:10]
        self.assertFalse(validate_review(p, r)["machine_valid"])

    def test_excerpt_metadata_rejected_even_with_recomputed_cache(self):
        p = self.packet()
        for field, value in (("start", True), ("start", -1), ("end", 100000), ("text", "")):
            bad = copy.deepcopy(p)
            bad["sources"][0]["excerpts"][0][field] = value
            bad["cache_key"] = hashlib.sha256(json.dumps({k: v for k, v in bad.items() if k != "cache_key"},
                                                        ensure_ascii=False, sort_keys=True,
                                                        separators=(",", ":")).encode()).hexdigest()
            self.assertFalse(validate_review(bad, self.result(bad))["machine_valid"])

    def test_excerpt_byte_boundaries_and_configuration(self):
        rows = [record(x, body="🙂中文" * 10000 + x) for x in "abc"]
        for cap in (24, 25, 100, 1500):
            p = build_review_plan(rows, [claim()], excerpt_bytes=cap)["packets"][0]
            for source in p["sources"]:
                self.assertLessEqual(sum(len(w["text"].encode()) for w in source["excerpts"]), cap)
            self.assertTrue(validate_review(p, self.result(p))["machine_valid"])
        for cap in (True, 0, -1, 23, 24.5, "1500", None):
            with self.assertRaises(ValueError):
                self.plan(excerpt_bytes=cap)


if __name__ == "__main__":
    unittest.main()
