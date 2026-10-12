"""Offline regressions for the million/thousand-million active score model."""
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_hot_contests.py"
SPEC = importlib.util.spec_from_file_location("two_tier_analysis", SCRIPT)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


class TwoTierAnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.weights = copy.deepcopy(analysis.DEFAULT_WEIGHTS)
        self.config = self.root / "consensus-weights.json"
        self.save(self.config, self.weights)
        configuration = patch.object(analysis, "WEIGHTS_PATH", self.config)
        configuration.start()
        self.addCleanup(configuration.stop)

    @staticmethod
    def save(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def snapshot(self, day, counts=None, holder_count=10):
        path = self.root / day
        path.mkdir()
        completed = day + "T16:30:00+08:00"
        manifest = {"run_id": day, "completed_at": completed,
                    "collection_complete": True, "scope": "synthetic public directory",
                    "stock_scope": "official truncated hot lists", "threshold": 1000,
                    "selection_rule": "participant_count > threshold", "failed_contest_count": 0,
                    "pending_contest_count": 0, "hot_contest_count": 1,
                    "collected_contest_count": 1, "stock_row_count": 1, "catalog_entry_count": 1}
        contest = {"contest_key": "synthetic-contest", "competition_name": "合成比赛",
                   "participant_count": 2000, "qualifies": True, "collection_status": "collected",
                   "source_updated_at": completed, "returned_stock_count": 1}
        row = {"contest_key": contest["contest_key"], "competition_name": contest["competition_name"],
               "market": "1", "stock_code": "000001", "stock_name": "合成股票",
               "holder_count": holder_count, "stock_rank": 1, "source_updated_at": completed}
        self.save(path / "manifest.json", manifest)
        self.save(path / "catalog.json", {"scope": manifest["scope"], "threshold": 1000, "items": [contest]})
        self.save(path / "all-hot-stocks.json", {"metadata": manifest, "records": [row]})
        snapshot = analysis.read_snapshot(path)
        metadata = {"schema_version": 1, "base_run_id": day,
                    "base_completed_at": completed, "base_fingerprint": analysis.snapshot_fingerprint(snapshot),
                    "collection_date": day, "completed_at": day + "T17:00:00+08:00",
                    "collection_complete": True, "member_count": 3,
                    "holdings_success_count": 3, "holdings_failed_count": 0}
        groups = {code: {"metadata": {**metadata, "group_code": code},
                         "contests": [{"contest_key": contest["contest_key"], "member_count": 3,
                                       "holdings_success_count": 3, "holdings_failed_count": 0,
                                       "ranking_complete": True, "collection_complete": True}],
                         "records": [{**row, "group_holder_count": count}]}
                  for code, count in (counts or {"100": 2, "1000": 1}).items()}
        payload = {"metadata": {**metadata, "weight_version": self.weights["version"],
                                "weights": self.weights["weights"], "active_group_codes": ["100", "1000"],
                                "weighting_model_version": analysis.WEIGHTING_MODEL_VERSION,
                                "selected_contest_count": 1, "completed_contest_count": 1}, "groups": groups}
        self.save(path / "group-holdings.json", payload)
        return snapshot, payload

    def report(self, snapshot, payload=None, baseline=None):
        if payload is not None:
            self.save(snapshot["path"] / "group-holdings.json", payload)
        return analysis.build_analysis(snapshot, baseline)

    def test_two_groups_score_and_metadata_keep_official_total_as_base(self):
        snapshot, _ = self.snapshot("2026-10-12")
        report = self.report(snapshot)
        row = report["ranking"][0]
        self.assertTrue(report["summary"]["group_weighting_available"])
        self.assertEqual(row["weighted_consensus_score"], 10 + 2 * 2 + 4 * 1)
        self.assertEqual(row["holder_count_sum"], 10)
        self.assertEqual(row["group_holder_counts"], {"100": 2, "1000": 1})
        self.assertEqual(report["metadata"]["active_group_codes"], ["100", "1000"])
        self.assertIn("全赛总量", report["metadata"]["group_weighting"]["scope"])

    def test_missing_either_group_never_infers_counts_or_score(self):
        for missing in ("100", "1000"):
            with self.subTest(missing=missing):
                snapshot, payload = self.snapshot("2026-10-" + ("12" if missing == "100" else "13"))
                del payload["groups"][missing]
                report = self.report(snapshot, payload)
                self.assertFalse(report["summary"]["group_weighting_available"])
                self.assertIsNone(report["ranking"][0]["weighted_consensus_score"])
                self.assertEqual(report["ranking"][0]["group_holder_counts"], {})

    def test_legacy_four_groups_do_not_enable_new_model_but_keep_raw_baseline(self):
        baseline, payload = self.snapshot("2026-10-09", {"1": 1, "10": 1, "100": 1, "1000": 1})
        payload["metadata"].update(weight_version="public-capital-groups-v1",
                                   weights={"1": 1, "10": 2, "100": 3, "1000": 5})
        self.save(baseline["path"] / "group-holdings.json", payload)
        current, _ = self.snapshot("2026-10-12", holder_count=12)
        chosen = analysis.choose_baseline(self.root, current, "2026-10-12")
        self.assertEqual(chosen["manifest"]["run_id"], "2026-10-09")
        report = self.report(current, baseline=chosen)
        row = report["ranking"][0]
        self.assertEqual(row["holder_count_sum_delta"], 2)
        self.assertIsNone(row["weighted_consensus_score_delta"])
        self.assertEqual(row["group_holder_counts_delta"], {})
        self.assertIsNone(row["rank_delta"])

    def test_old_weight_stamp_with_only_two_payload_groups_does_not_enable_new_model(self):
        snapshot, payload = self.snapshot("2026-10-12")
        payload["metadata"].update(weight_version="public-capital-groups-v1",
                                   weights={"1": 1, "10": 2, "100": 3, "1000": 5})
        self.assertFalse(self.report(snapshot, payload)["summary"]["group_weighting_available"])

    def test_two_group_model_or_active_codes_mismatch_rejects_weighting(self):
        snapshot, payload = self.snapshot("2026-10-12")
        for key, value in (("active_group_codes", ["1", "10", "100", "1000"]),
                           ("weighting_model_version", "public-capital-groups-v1")):
            with self.subTest(key=key):
                invalid = copy.deepcopy(payload)
                invalid["metadata"][key] = value
                self.assertFalse(self.report(snapshot, invalid)["summary"]["group_weighting_available"])

    def test_same_model_daily_count_and_score_deltas_are_observed(self):
        baseline, _ = self.snapshot("2026-10-09", {"100": 1, "1000": 1})
        current, _ = self.snapshot("2026-10-12")
        report = self.report(current, baseline=baseline)
        row = report["ranking"][0]
        self.assertEqual(row["group_holder_counts_delta"], {"100": 1, "1000": 0})
        self.assertEqual(row["weighted_consensus_score_delta"], 2)
        self.assertTrue(report["comparison"]["weighted_score_comparable"])

    def test_weights_filter_removed_labels_and_reject_removed_keys(self):
        weights = analysis.validate_weights({**self.weights, "labels": {"1": "旧标签", "10": "旧标签"}})
        self.assertEqual(weights["labels"], {"100": "百万组", "1000": "千万组"})
        for values in ({"1": 1, "10": 2, "100": 3, "1000": 5},
                       {"100": 5, "1000": 5}, {"100": 6, "1000": 5}):
            with self.subTest(weights=values), self.assertRaises(ValueError):
                analysis.validate_weights({**self.weights, "weights": values})

    def test_markdown_and_csv_expose_only_active_group_columns(self):
        baseline, _ = self.snapshot("2026-10-09", {"100": 1, "1000": 1})
        current, _ = self.snapshot("2026-10-12")
        report = self.report(current, baseline=baseline)
        output = analysis.write_report(report, self.root / "reports", publish=False)
        markdown = (output / "daily-review.md").read_text(encoding="utf-8")
        self.assertNotIn("新手组", markdown)
        self.assertNotIn("十万组", markdown)
        for name in ("hot-stock-ranking.csv", "changes.csv"):
            header = (output / name).read_text(encoding="utf-8-sig").splitlines()[0].split(",")
            self.assertIn("group_holder_count_100", header)
            self.assertIn("group_holder_count_1000", header)
            self.assertNotIn("group_holder_count_1", header)
            self.assertNotIn("group_holder_count_10", header)


if __name__ == "__main__":
    unittest.main()
