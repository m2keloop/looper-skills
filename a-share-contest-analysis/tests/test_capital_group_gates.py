"""Offline regressions for the all-capital-tier weighting requirement."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_hot_contests.py"
SPEC = importlib.util.spec_from_file_location("capital_gate_analysis", SCRIPT)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


class CapitalGroupGateTests(unittest.TestCase):
    def test_only_thousand_group_with_matching_four_tier_stamp_cannot_enable_weighting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            completed = "2026-10-10T16:30:00+08:00"
            config = {"version": "public-large-capital-groups-v2", "weights": {"100": 3, "1000": 5}}
            old_stamp = {"version": "public-capital-groups-v1", "weights": {"1": 1, "10": 2, "100": 3, "1000": 5}}
            config_path = root / "consensus-weights.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            manifest = {"run_id": "only-thousand-fixture", "completed_at": completed,
                        "collection_complete": True, "scope": "fixture public directory",
                        "stock_scope": "official truncated hot lists", "threshold": 1000,
                        "selection_rule": "participant_count > threshold", "failed_contest_count": 0,
                        "pending_contest_count": 0, "hot_contest_count": 1,
                        "collected_contest_count": 1, "stock_row_count": 1, "catalog_entry_count": 1}
            contest = {"contest_key": "fixture-contest", "competition_name": "测试比赛",
                       "participant_count": 2000, "qualifies": True, "collection_status": "collected",
                       "contest_status": 0, "source_updated_at": completed, "returned_stock_count": 1}
            record = {"contest_key": contest["contest_key"], "competition_name": contest["competition_name"],
                      "market": "1", "stock_code": "000001", "stock_name": "测试股票",
                      "holder_count": 10, "stock_rank": 1, "source_updated_at": completed}
            supplement = {
                "metadata": {"schema_version": 1, "base_run_id": manifest["run_id"],
                             "group_code": "1000", "collection_complete": True,
                             "completed_at": completed, "weight_version": old_stamp["version"],
                             "weights": old_stamp["weights"]},
                "contests": [{"contest_key": contest["contest_key"], "member_count": 2,
                              "holdings_success_count": 2, "holdings_failed_count": 0,
                              "ranking_complete": True, "collection_complete": True}],
                "records": [{**record, "thousand_holder_count": 2}],
            }
            for filename, payload in {
                "manifest.json": manifest,
                "catalog.json": {"scope": manifest["scope"], "threshold": 1000, "items": [contest]},
                "all-hot-stocks.json": {"metadata": manifest, "records": [record]},
                "thousand-group.json": supplement,
            }.items():
                (root / filename).write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(analysis, "WEIGHTS_PATH", config_path):
                snapshot = analysis.read_snapshot(root)
                group = analysis.load_group_collection(snapshot, analysis.load_weights())
                self.assertFalse(group["available"])
                self.assertFalse(group["collection_complete"])
                self.assertTrue(group["baseline_eligible"])
                self.assertEqual(group["records"][("fixture-contest", "1", "000001")]["thousand_holder_count"], 2)
                report = analysis.build_analysis(snapshot)
            self.assertFalse(report["summary"]["group_weighting_available"])
            self.assertEqual(report["metadata"]["ranking_method"], "contest_coverage")
            self.assertIsNone(report["ranking"][0]["weighted_consensus_score"])
            self.assertEqual(report["ranking"][0]["group_holder_counts"], {})


if __name__ == "__main__":
    unittest.main()
