"""Offline checks for exchange holidays and the pre-network synchronization guard."""
import argparse
import contextlib
import importlib.util
import io
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("trading_day_guard_sync", ROOT / "scripts" / "sync_hot_contests.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)
calendar = sys.modules["trading_calendar"]
REAL_COMMAND = sync.command


class TradingCalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "calendar.json"
        self.payload = {"schema_version": 1, "years": {"2026": {
            "holidays": ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"],
            "sources": ["https://www.sse.com.cn/"]}}}
        self.write_calendar()

    def write_calendar(self):
        self.path.write_text(json.dumps(self.payload), encoding="utf-8")

    def test_weekday_outside_holidays_is_trading_day(self):
        self.assertTrue(calendar.trading_day_status(date(2026, 10, 9), self.path)[0])

    def test_weekend_and_makeup_workday_are_closed(self):
        for day in (date(2026, 10, 10), date(2026, 10, 11), date(2026, 9, 20)):
            with self.subTest(day=day):
                allowed, reason = calendar.trading_day_status(day, self.path)
                self.assertFalse(allowed)
                self.assertIn("周末", reason)

    def test_official_holidays_are_closed_on_weekdays(self):
        for day in (date(2026, 10, 1), date(2026, 10, 7)):
            with self.subTest(day=day):
                allowed, reason = calendar.trading_day_status(day, self.path)
                self.assertFalse(allowed)
                self.assertIn("节假日", reason)

    def test_saved_official_calendar_october_reopening_and_weekend(self):
        for day, expected in ((date(2026, 10, 8), True),
                              (date(2026, 10, 10), False),
                              (date(2026, 10, 12), True)):
            with self.subTest(day=day):
                self.assertEqual(calendar.trading_day_status(day)[0], expected)

    def test_unknown_weekday_year_fails_closed(self):
        with self.assertRaises(calendar.CalendarUnavailable):
            calendar.trading_day_status(date(2027, 1, 4), self.path)

    def test_missing_or_corrupt_calendar_fails_closed_on_weekday(self):
        self.path.unlink()
        with self.assertRaises(calendar.CalendarUnavailable):
            calendar.trading_day_status(date(2026, 10, 9), self.path)
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(calendar.CalendarUnavailable):
            calendar.trading_day_status(date(2026, 10, 9), self.path)

    def test_invalid_calendar_metadata_fails_closed(self):
        for mutation in (lambda: self.payload.update(schema_version=99),
                         lambda: self.payload["years"]["2026"].update(sources=[]),
                         lambda: self.payload["years"]["2026"].update(holidays=["2027-01-01"])):
            with self.subTest(mutation=mutation):
                self.payload = {"schema_version": 1, "years": {"2026": {
                    "holidays": [], "sources": ["https://www.sse.com.cn/"]}}}
                mutation()
                self.write_calendar()
                with self.assertRaises(calendar.CalendarUnavailable):
                    calendar.trading_day_status(date(2026, 10, 9), self.path)

    def test_weekend_does_not_require_calendar_or_network(self):
        self.path.unlink()
        self.assertFalse(calendar.trading_day_status(date(2027, 1, 2), self.path)[0])


class TradingDaySyncGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.snapshots = self.root / "snapshots"
        self.snapshots.mkdir()
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.old_pointer = b'{"relative_path":"previous-complete"}\n'
        self.old_index = b"<html>previous complete report</html>\n"
        (self.reports / "latest.json").write_bytes(self.old_pointer)
        (self.reports / "index.html").write_bytes(self.old_index)
        self.args = argparse.Namespace(analyze_only=False, hot_only=True, snapshot=None,
                                       snapshots=self.snapshots, report_root=self.reports,
                                       registry=self.root / "registry.json", threshold=1000, interval=2.0)

    def clock(self, year, month, day):
        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(year, month, day, 16, 30, tzinfo=tz)
        return patch.object(sync, "datetime", FixedClock)

    def invoke(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return sync.sync(self.args)

    def status(self):
        return json.loads((self.reports / "sync-latest.json").read_text())

    def assert_published_report_preserved(self):
        self.assertEqual((self.reports / "latest.json").read_bytes(), self.old_pointer)
        self.assertEqual((self.reports / "index.html").read_bytes(), self.old_index)

    def test_weekend_skips_every_downstream_command_and_preserves_report(self):
        with self.clock(2026, 10, 10), patch.object(sync, "command") as command:
            self.assertEqual(self.invoke(), 0)
            command.assert_not_called()
        self.assert_published_report_preserved()
        self.assertEqual(list(self.snapshots.iterdir()), [])
        status = self.status()
        self.assertEqual(status["stage"], "skipped")
        self.assertTrue(status["skipped"])
        self.assertFalse(status["complete"])
        self.assertEqual(status["trading_date"], "2026-10-10")
        self.assertIn("周末", status["skip_reason"])

    def test_exchange_holiday_skips_every_downstream_command(self):
        with self.clock(2026, 10, 1), patch.object(sync, "command") as command:
            self.assertEqual(self.invoke(), 0)
            command.assert_not_called()
        self.assert_published_report_preserved()
        self.assertIn("节假日", self.status()["skip_reason"])

    def test_unknown_year_stops_before_network_and_preserves_report(self):
        with self.clock(2027, 1, 4), patch.object(sync, "command") as command:
            self.assertEqual(self.invoke(), 2)
            command.assert_not_called()
        self.assert_published_report_preserved()
        status = self.status()
        self.assertEqual(status["stage"], "trading_day_check")
        self.assertIn("2027", status["error"])

    def test_trading_day_at_1630_enters_collection(self):
        # The collector is replaced entirely: no network request can occur.
        with self.clock(2026, 10, 12), patch.object(sync, "command", return_value=
                subprocess.CompletedProcess([], 7, "", "fixture stop")) as command:
            self.assertEqual(self.invoke(), 7)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0], "collect_hot_contests.py")
        self.assert_published_report_preserved()
        self.assertEqual(self.status()["stage"], "collection")

    def invoke_with_day_transition(self, initial_day, resumed_day):
        current = [datetime.combine(initial_day, datetime.min.time(), tzinfo=sync.TZ)]

        class RollingClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return current[0]

        process = Mock(returncode=130)

        def communicate(timeout=None):
            if timeout is not None:
                current[0] = datetime.combine(resumed_day, datetime.min.time(), tzinfo=sync.TZ)
                raise subprocess.TimeoutExpired("fixture collector", timeout)
            return "checkpoint saved\n", ""

        process.communicate.side_effect = communicate
        with patch.object(sync, "datetime", RollingClock), \
                patch.object(sync.subprocess, "Popen", return_value=process) as popen, \
                patch.object(sync.subprocess, "run") as offline_run:
            result = self.invoke()
            self.assertEqual(popen.call_count, 1)
            self.assertTrue(popen.call_args.args[0][1].endswith("collect_hot_contests.py"))
            offline_run.assert_not_called()
        process.send_signal.assert_called_once_with(signal.SIGINT)
        self.assertEqual(process.communicate.call_args_list, [call(timeout=30), call()])
        return result

    def test_running_collection_enters_weekend_and_waits_for_checkpoint_before_skipping(self):
        self.assertEqual(self.invoke_with_day_transition(date(2026, 10, 9), date(2026, 10, 10)), 0)
        self.assert_published_report_preserved()
        status = self.status()
        self.assertTrue(status["skipped"])
        self.assertEqual(status["stage"], "skipped")
        self.assertFalse(status["complete"])
        self.assertIn("采集中进入休市日", status["skip_reason"])

    def test_running_collection_enters_unknown_year_and_stops_before_publication(self):
        self.assertEqual(self.invoke_with_day_transition(date(2026, 12, 31), date(2027, 1, 1)), 2)
        self.assert_published_report_preserved()
        status = self.status()
        self.assertFalse(status["complete"])
        self.assertEqual(status["stage"], "collection")
        self.assertIn("2027", status["error"])

    def test_analyze_only_on_weekend_bypasses_guard_and_publishes_offline(self):
        snapshot = self.snapshots / "fixture"
        snapshot.mkdir()
        completed = "2026-10-09T16:30:00+08:00"
        manifest = {"run_id": "fixture-calendar", "completed_at": completed,
                    "collection_complete": True, "scope": "fixture public directory",
                    "stock_scope": "official truncated hot lists", "threshold": 1000,
                    "selection_rule": "participant_count > threshold", "failed_contest_count": 0,
                    "pending_contest_count": 0, "hot_contest_count": 1,
                    "collected_contest_count": 1, "stock_row_count": 1, "catalog_entry_count": 1}
        contest = {"contest_key": "fixture-contest", "competition_name": "测试比赛",
                   "participant_count": 2000, "participant_count_checked_at": completed,
                   "qualifies": True, "collection_status": "collected", "contest_status": 0,
                   "source_updated_at": completed, "returned_stock_count": 1}
        record = {"contest_key": "fixture-contest", "competition_name": "测试比赛",
                  "market": "1", "stock_code": "000001", "stock_name": "测试股票",
                  "holder_count": 10, "stock_rank": 1, "source_updated_at": completed}
        for name, payload in {"manifest.json": manifest,
                              "catalog.json": {"scope": manifest["scope"], "threshold": 1000, "items": [contest]},
                              "all-hot-stocks.json": {"metadata": manifest, "records": [record]}}.items():
            (snapshot / name).write_text(json.dumps(payload), encoding="utf-8")
        self.args.analyze_only = True
        self.args.snapshot = snapshot

        def offline_command(name, arguments):
            if name not in ("analyze_hot_contests.py", "render_hot_contest_report.py"):
                raise AssertionError("Offline analysis must not invoke any network collector")
            return REAL_COMMAND(name, arguments)

        with self.clock(2026, 10, 10), patch.object(sync, "trading_day_status") as guard, \
                patch.object(sync, "command", side_effect=offline_command) as command:
            self.assertEqual(self.invoke(), 0)
            guard.assert_not_called()
            self.assertEqual([call.args[0] for call in command.call_args_list],
                             ["analyze_hot_contests.py", "render_hot_contest_report.py"])
        pointer = json.loads((self.reports / "latest.json").read_text())
        self.assertTrue((self.reports / pointer["relative_path"] / "index.html").is_file())
        self.assertTrue(self.status()["complete"])


class NetworkCommandGuardTests(unittest.TestCase):
    def invoke(self, script):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return sync.command(script, ["--fixture"])

    def test_completed_network_process_returns_its_actual_result(self):
        process = Mock(returncode=7)
        process.communicate.return_value = ("collected output\n", "source error\n")
        with patch.object(sync, "trading_day_status", return_value=(True, "A 股交易日")), \
                patch.object(sync.subprocess, "Popen", return_value=process) as popen, \
                patch.object(sync.subprocess, "run") as offline_run:
            result = self.invoke("collect_hot_contests.py")
            offline_run.assert_not_called()
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "collected output\n")
        self.assertEqual(result.stderr, "source error\n")
        self.assertEqual(result.args, popen.call_args.args[0])
        process.communicate.assert_called_once_with(timeout=30)
        process.send_signal.assert_not_called()

    def test_timed_wait_keeps_collecting_while_day_is_still_open(self):
        process = Mock(returncode=0)
        process.communicate.side_effect = [subprocess.TimeoutExpired("fixture collector", 30), ("done", "")]
        with patch.object(sync, "trading_day_status", return_value=(True, "A 股交易日")) as guard, \
                patch.object(sync.subprocess, "Popen", return_value=process):
            result = self.invoke("collect_capital_groups.py")
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(process.communicate.call_args_list, [call(timeout=30), call(timeout=30)])
        process.send_signal.assert_not_called()

    def test_offline_scripts_use_run_without_calendar_checks(self):
        for script in ("analyze_hot_contests.py", "render_hot_contest_report.py"):
            with self.subTest(script=script):
                expected = subprocess.CompletedProcess([script], 0, "offline result\n", "")
                with patch.object(sync, "trading_day_status") as guard, \
                        patch.object(sync.subprocess, "Popen") as popen, \
                        patch.object(sync.subprocess, "run", return_value=expected) as run:
                    self.assertIs(self.invoke(script), expected)
                    run.assert_called_once_with(
                        [sys.executable, str(sync.SCRIPTS / script), "--fixture"],
                        capture_output=True, text=True)
                    guard.assert_not_called()
                    popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
