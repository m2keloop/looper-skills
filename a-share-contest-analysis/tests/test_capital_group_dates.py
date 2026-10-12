"""Synthetic, offline regressions for two-tier snapshot date and identity gates."""
import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_hot_contests.py"
SPEC = importlib.util.spec_from_file_location("capital_date_analysis", SCRIPT)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


class CapitalGroupDateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.completed = "2026-10-12T16:30:00+08:00"
        self.weights = {"version": "two-tier-date-fixture",
                        "weights": {"100": 3, "1000": 5}}
        weights_path = self.root / "consensus-weights.json"
        self.save(weights_path, self.weights)
        weights_patch = patch.object(analysis, "WEIGHTS_PATH", weights_path)
        weights_patch.start()
        self.addCleanup(weights_patch.stop)
        manifest = {"run_id": "synthetic-two-tier-run", "completed_at": self.completed,
                    "collection_complete": True, "scope": "synthetic public directory",
                    "stock_scope": "official truncated hot lists", "threshold": 1000,
                    "selection_rule": "participant_count > threshold", "failed_contest_count": 0,
                    "pending_contest_count": 0, "hot_contest_count": 1,
                    "collected_contest_count": 1, "stock_row_count": 1, "catalog_entry_count": 1}
        contest = {"contest_key": "synthetic-contest", "competition_name": "合成比赛",
                   "participant_count": 2000, "qualifies": True, "collection_status": "collected",
                   "source_updated_at": self.completed, "returned_stock_count": 1}
        record = {"contest_key": contest["contest_key"], "competition_name": contest["competition_name"],
                  "market": "1", "stock_code": "000001", "stock_name": "合成股票",
                  "holder_count": 10, "stock_rank": 1, "source_updated_at": self.completed}
        for filename, payload in {
            "manifest.json": manifest,
            "catalog.json": {"scope": manifest["scope"], "threshold": 1000, "items": [contest]},
            "all-hot-stocks.json": {"metadata": manifest, "records": [record]},
        }.items():
            self.save(self.root / filename, payload)
        self.snapshot = analysis.read_snapshot(self.root)
        digest = hashlib.sha256()
        for filename in ("manifest.json", "catalog.json", "all-hot-stocks.json"):
            digest.update((self.root / filename).read_bytes())
        identity = {"schema_version": 1, "base_run_id": manifest["run_id"],
                    "base_completed_at": self.completed, "base_fingerprint": digest.hexdigest(),
                    "collection_date": "2026-10-12", "completed_at": "2026-10-12T17:00:00+08:00",
                    "collection_complete": True, "member_count": 2,
                    "holdings_success_count": 2, "holdings_failed_count": 0}
        groups = {code: {"metadata": {**identity, "group_code": code},
                         "contests": [{"contest_key": contest["contest_key"], "member_count": 2,
                                       "holdings_success_count": 2, "holdings_failed_count": 0,
                                       "ranking_complete": True, "collection_complete": True}],
                         "records": [{**record, "group_holder_count": 1}]}
                  for code in self.weights["weights"]}
        self.valid = {"metadata": {**identity, "weight_version": self.weights["version"],
                                   "weights": self.weights["weights"], "selected_contest_count": 1,
                                   "completed_contest_count": 1}, "groups": groups}

    @staticmethod
    def save(path, payload):
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def report(self, payload):
        self.save(self.root / "group-holdings.json", payload)
        return analysis.build_analysis(self.snapshot)

    def assert_rejected(self, payload):
        report = self.report(payload)
        self.assertFalse(report["summary"]["group_weighting_available"])
        self.assertEqual(report["metadata"]["ranking_method"], "contest_coverage")
        row = report["ranking"][0]
        self.assertEqual(row["stock_code"], "000001")
        self.assertEqual(row["holder_count_sum"], 10)
        self.assertIsNone(row["weighted_consensus_score"])
        self.assertEqual(row["group_holder_counts"], {})
        self.assertFalse(report["metadata"]["group_collection"]["baseline_eligible"])
        self.assertTrue(report["metadata"]["group_collection"]["warnings"])

    def test_valid_same_date_both_active_groups_enable_weighting(self):
        report = self.report(self.valid)
        self.assertTrue(report["summary"]["group_weighting_available"])
        self.assertEqual(report["metadata"]["ranking_method"], "weighted_consensus")
        self.assertEqual(report["ranking"][0]["weighted_consensus_score"], 16)
        self.assertEqual(report["ranking"][0]["group_holder_counts"],
                         {"100": 1, "1000": 1})

    def test_completed_dates_are_compared_in_shanghai_timezone(self):
        payload = copy.deepcopy(self.valid)
        payload["metadata"]["completed_at"] = "2026-10-12T09:00:00Z"
        for group in payload["groups"].values():
            group["metadata"]["completed_at"] = "2026-10-12T08:40:00Z"
        report = self.report(payload)
        self.assertTrue(report["summary"]["group_weighting_available"])

    def test_collection_date_must_match_base_for_top_level_and_each_tier(self):
        for target in ("merged", "100", "1000"):
            with self.subTest(target=target):
                payload = copy.deepcopy(self.valid)
                meta = payload["metadata"] if target == "merged" else payload["groups"][target]["metadata"]
                meta["collection_date"] = "2026-10-13"
                self.assert_rejected(payload)

    def test_actual_completed_date_must_match_base_for_top_level_and_each_tier(self):
        for target in ("merged", "100", "1000"):
            with self.subTest(target=target):
                payload = copy.deepcopy(self.valid)
                meta = payload["metadata"] if target == "merged" else payload["groups"][target]["metadata"]
                meta["completed_at"] = "2026-10-13T17:00:00+08:00"
                self.assert_rejected(payload)

    def test_matching_utc_calendar_date_cannot_hide_next_shanghai_date(self):
        payload = copy.deepcopy(self.valid)
        payload["groups"]["1000"]["metadata"]["completed_at"] = "2026-10-12T17:00:00Z"
        self.assert_rejected(payload)

    def test_missing_collection_or_completion_date_in_any_tier_rejects_weighting(self):
        for target in ("merged", "100", "1000"):
            for field in ("collection_date", "completed_at"):
                with self.subTest(target=target, field=field):
                    payload = copy.deepcopy(self.valid)
                    meta = payload["metadata"] if target == "merged" else payload["groups"][target]["metadata"]
                    del meta[field]
                    self.assert_rejected(payload)

    def test_invalid_dates_or_timezone_free_completion_reject_weighting(self):
        for field, value in (("collection_date", "20261012"), ("collection_date", "2026-02-30"),
                             ("collection_date", None), ("completed_at", "2026-10-12T17:00:00")):
            with self.subTest(field=field, value=value):
                payload = copy.deepcopy(self.valid)
                payload["groups"]["100"]["metadata"][field] = value
                self.assert_rejected(payload)

    def test_present_base_timestamp_and_fingerprint_must_match_for_every_tier(self):
        for target in ("merged", "100", "1000"):
            for field, value in (("base_completed_at", "2026-10-12T16:31:00+08:00"),
                                 ("base_fingerprint", "f" * 64), ("base_run_id", "other-run")):
                with self.subTest(target=target, field=field):
                    payload = copy.deepcopy(self.valid)
                    meta = payload["metadata"] if target == "merged" else payload["groups"][target]["metadata"]
                    meta[field] = value
                    self.assert_rejected(payload)

    def test_base_file_byte_change_invalidates_existing_supplement_identity(self):
        catalog_path = self.root / "catalog.json"
        catalog_path.write_text(catalog_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.snapshot = analysis.read_snapshot(self.root)
        self.assert_rejected(self.valid)

    def test_legacy_same_day_groups_without_optional_identity_stamp_still_validate_dates(self):
        payload = copy.deepcopy(self.valid)
        for meta in [payload["metadata"], *(g["metadata"] for g in payload["groups"].values())]:
            del meta["base_completed_at"]
            del meta["base_fingerprint"]
        report = self.report(payload)
        self.assertTrue(report["summary"]["group_weighting_available"])


if __name__ == "__main__":
    unittest.main()
