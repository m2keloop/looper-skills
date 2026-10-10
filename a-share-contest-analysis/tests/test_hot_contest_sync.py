"""Offline workflow safety checks with self-contained temporary snapshots."""
import argparse
import contextlib
import fcntl
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_hot_contests.py"
SPEC = importlib.util.spec_from_file_location("hot_contest_sync", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)
REAL_COMMAND = sync.command


class SyncSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.snapshots = self.root / "snapshots"
        self.snapshot = self.snapshots / "current"
        self.snapshot.mkdir(parents=True)
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.old_pointer = b'{"relative_path":"previous-complete"}\n'
        self.old_index = b"<html>previous complete report</html>\n"
        (self.reports / "latest.json").write_bytes(self.old_pointer)
        (self.reports / "index.html").write_bytes(self.old_index)
        completed = datetime.now(sync.TZ).isoformat(timespec="seconds")
        manifest = {"run_id": "fixture-current", "completed_at": completed,
                    "collection_complete": True, "scope": "fixture public directory",
                    "stock_scope": "official truncated hot lists", "threshold": 1000,
                    "selection_rule": "participant_count > threshold", "failed_contest_count": 0,
                    "pending_contest_count": 0, "hot_contest_count": 1,
                    "collected_contest_count": 1, "stock_row_count": 1, "catalog_entry_count": 1}
        contest = {"contest_key": "fixture-contest", "competition_name": "测试比赛",
                   "participant_count": 2000, "participant_count_checked_at": completed,
                   "qualifies": True, "collection_status": "collected", "contest_status": 0,
                   "source_updated_at": completed, "returned_stock_count": 1}
        record = {"contest_key": contest["contest_key"], "competition_name": contest["competition_name"],
                  "market": "1", "stock_code": "000001", "stock_name": "测试股票",
                  "holder_count": 10, "stock_rank": 1, "source_updated_at": completed}
        payloads = {"manifest.json": manifest,
                    "catalog.json": {"scope": manifest["scope"], "threshold": 1000, "items": [contest]},
                    "all-hot-stocks.json": {"metadata": manifest, "records": [record]}}
        for name, payload in payloads.items():
            (self.snapshot / name).write_text(json.dumps(payload), encoding="utf-8")
        self.args = argparse.Namespace(analyze_only=True, snapshot=self.snapshot,
                                       snapshots=self.snapshots, report_root=self.reports,
                                       registry=self.root / "unused-registry.json", threshold=1000, interval=2.0)
        # Real subprocesses here are restricted to the two offline scripts.
        def offline_command(name, arguments):
            if name not in ("analyze_hot_contests.py", "render_hot_contest_report.py"):
                raise AssertionError("The test must never invoke the network collector")
            return REAL_COMMAND(name, arguments)
        self.offline_command = offline_command
        guard = patch.object(sync, "command", side_effect=offline_command)
        guard.start()
        self.addCleanup(guard.stop)
        # These cases test downstream collection/publication failures, regardless
        # of the calendar date when the test suite happens to run.
        trading_guard = patch.object(sync, "trading_day_status", return_value=(True, "A 股交易日"))
        trading_guard.start()
        self.addCleanup(trading_guard.stop)

    def invoke(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return sync.sync(self.args)

    def assert_preserved(self):
        self.assertEqual((self.reports / "latest.json").read_bytes(), self.old_pointer)
        self.assertEqual((self.reports / "index.html").read_bytes(), self.old_index)
        status = json.loads((self.reports / "sync-latest.json").read_text())
        self.assertFalse(status["complete"])

    def test_incomplete_snapshot_keeps_published_report(self):
        path = self.snapshot / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["collection_complete"] = False
        path.write_text(json.dumps(manifest))
        self.assertEqual(self.invoke(), 1)
        self.assert_preserved()

    def test_invalid_snapshot_keeps_published_report(self):
        (self.snapshot / "all-hot-stocks.json").write_text("{invalid JSON")
        self.assertEqual(self.invoke(), 1)
        self.assert_preserved()

    def test_renderer_failure_keeps_published_report(self):
        def command(name, arguments):
            if name == "render_hot_contest_report.py":
                return subprocess.CompletedProcess(arguments, 7, "", "Renderer failed")
            return self.offline_command(name, arguments)
        with patch.object(sync, "command", side_effect=command):
            self.assertEqual(self.invoke(), 7)
        self.assert_preserved()

    def test_renderer_without_html_keeps_published_report(self):
        def command(name, arguments):
            if name == "render_hot_contest_report.py":
                return subprocess.CompletedProcess(arguments, 0, "", "")
            return self.offline_command(name, arguments)
        with patch.object(sync, "command", side_effect=command):
            self.assertEqual(self.invoke(), 1)
        self.assert_preserved()

    def test_collection_failure_keeps_published_report(self):
        self.args.analyze_only = False
        with patch.object(sync, "command", return_value=subprocess.CompletedProcess([], 2, "", "Access limit")) as command:
            self.assertEqual(self.invoke(), 2)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0], "collect_hot_contests.py")
        self.assert_preserved()

    def test_thousand_group_failure_keeps_published_report(self):
        self.args.analyze_only = False
        calls = []
        def command(name, arguments):
            calls.append(name)
            if name == "collect_hot_contests.py":
                return subprocess.CompletedProcess(arguments, 0,
                    json.dumps({"run_directory": str(self.snapshot)}) + "\n", "")
            if name == "collect_thousand_group.py":
                return subprocess.CompletedProcess(arguments, 2, "", "Incomplete public holdings")
            raise AssertionError("Analysis must not run after incomplete thousand-group collection")
        with patch.object(sync, "command", side_effect=command):
            self.assertEqual(self.invoke(), 2)
        self.assertEqual(calls, ["collect_hot_contests.py", "collect_thousand_group.py"])
        self.assertEqual(json.loads((self.reports / "sync-latest.json").read_text())["stage"],
                         "thousand_group_collection")
        self.assert_preserved()

    def test_live_sync_without_valid_four_groups_keeps_published_report(self):
        self.args.analyze_only = False
        calls = []

        def command(name, arguments):
            calls.append(name)
            if name == "collect_hot_contests.py":
                return subprocess.CompletedProcess(arguments, 0,
                    json.dumps({"run_directory": str(self.snapshot)}) + "\n", "")
            if name in ("collect_thousand_group.py", "collect_capital_groups.py"):
                # A successful process exit does not establish that the saved
                # supplement passed the analyzer's four-tier validation.
                return subprocess.CompletedProcess(arguments, 0, "", "")
            if name == "render_hot_contest_report.py":
                raise AssertionError("An invalid four-tier report must not reach rendering")
            return self.offline_command(name, arguments)

        with patch.object(sync, "command", side_effect=command):
            self.assertEqual(self.invoke(), 2)
        self.assertEqual(calls, ["collect_hot_contests.py", "collect_thousand_group.py",
                                 "collect_capital_groups.py", "analyze_hot_contests.py"])
        status = json.loads((self.reports / "sync-latest.json").read_text())
        self.assertEqual(status["stage"], "analysis")
        self.assertEqual(status["exit_code"], 2)
        self.assert_preserved()

    def test_overlapping_workflow_exits_3(self):
        argv = ["sync_hot_contests.py", "--analyze-only", "--snapshot", str(self.snapshot),
                "--snapshots", str(self.snapshots), "--report-root", str(self.reports)]
        with (self.reports / ".daily-sync.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sync.main(), 3)
        self.assertEqual((self.reports / "latest.json").read_bytes(), self.old_pointer)
        self.assertEqual((self.reports / "index.html").read_bytes(), self.old_index)
        self.assertFalse((self.reports / "sync-latest.json").exists())

    def fail_pointer_write(self):
        original_save = sync.save
        def save(path, value):
            if path == self.reports / "latest.json":
                raise OSError("Simulated final pointer write failure")
            return original_save(path, value)
        return patch.object(sync, "save", side_effect=save)

    def test_pointer_write_failure_rolls_back_html_entry(self):
        with self.fail_pointer_write():
            self.assertEqual(self.invoke(), 1)
        self.assert_preserved()

    def test_initial_pointer_write_failure_leaves_no_published_entry(self):
        (self.reports / "index.html").unlink()
        (self.reports / "latest.json").unlink()
        with self.fail_pointer_write():
            self.assertEqual(self.invoke(), 1)
        self.assertFalse((self.reports / "latest.json").exists())
        self.assertFalse((self.reports / "index.html").exists())
        self.assertFalse(json.loads((self.reports / "sync-latest.json").read_text())["complete"])

    def test_success_publishes_only_a_complete_rendered_report(self):
        self.assertEqual(self.invoke(), 0)
        pointer = json.loads((self.reports / "latest.json").read_text())
        report = self.reports / pointer["relative_path"]
        self.assertTrue((report / "index.html").is_file())
        self.assertTrue((report / "analysis.json").is_file())
        self.assertEqual(pointer["html_path"], pointer["relative_path"] + "/index.html")
        self.assertIn(pointer["html_path"], (self.reports / "index.html").read_text())
        status = json.loads((self.reports / "sync-latest.json").read_text())
        self.assertTrue(status["complete"])
        self.assertEqual(status["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
