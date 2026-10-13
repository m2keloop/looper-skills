"""Offline checks for bounded concurrent collection and coordinated checkpoints."""
import contextlib
import io
import json
import subprocess
import threading
import time
import unittest
from unittest import mock

import test_capital_recovery as recovery

collector = recovery.collector
FakeClient = recovery.FakeClient


class ParallelHoldingsTests(unittest.TestCase):
    setUp = recovery.CapitalRecoveryTests.setUp

    def checkpoint(self, members, holdings=None):
        path, checkpoint = collector.load_checkpoint(self.snapshot, '100')
        checkpoint['contests']['test'] = {
            'members': members, 'holdings': holdings or {},
            'ranking_complete': True, 'ranking_status': 'complete', 'last_page': 1}
        collector.save(path, checkpoint)
        return path

    def collect(self, clients):
        with mock.patch.object(collector, 'PublicClient', side_effect=clients), \
                contextlib.redirect_stdout(io.StringIO()):
            return collector.collect_group(self.snapshot, '100', FakeClient(), 2, workers=2)

    def test_two_readers_overlap_skip_success_and_commit_on_main_thread(self):
        path = self.checkpoint(['saved', 'a', 'b', 'c', 'd'], {
            'saved': {'status': 'success', 'stock_codes': []}})
        barrier = threading.Barrier(2, timeout=2)
        lock = threading.Lock()
        active, maximum = 0, 0

        def overlap():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            barrier.wait()
            time.sleep(.005)
            with lock:
                active -= 1

        clients = [FakeClient(on_position=overlap), FakeClient(on_position=overlap)]
        checkpoint_threads = []
        original_save = collector.save

        def record_save(target, value):
            if target == path:
                checkpoint_threads.append(threading.current_thread().name)
            original_save(target, value)

        with mock.patch.object(collector, 'save', side_effect=record_save):
            result = self.collect(clients)
        calls = [call for client in clients for call in client.calls]
        self.assertEqual(maximum, 2)
        self.assertEqual({call[1] for call in calls}, {'a', 'b', 'c', 'd'})
        self.assertEqual(len(calls), 4)
        self.assertEqual(set(checkpoint_threads), {'MainThread'})
        self.assertTrue(result['metadata']['collection_complete'])
        self.assertEqual(result['metadata']['holding_workers'], 2)
        saved = json.loads(path.read_text())
        self.assertEqual(len(saved['request_log']), 4)
        self.assertEqual(set(saved['contests']['test']['holdings']), {'saved', 'a', 'b', 'c', 'd'})

    def test_access_limit_stops_next_batch_and_keeps_cancelled_members_pending(self):
        path = self.checkpoint(['a', 'b', 'c', 'd'])
        clients = [FakeClient(failures={('position_by_share', 'a'): collector.AccessLimited('HTTP 429')}),
                   FakeClient(failures={('position_by_share', 'b'): collector.RequestsStopped('stopped')})]
        result = self.collect(clients)
        self.assertFalse(result['metadata']['collection_complete'])
        self.assertTrue(result['metadata']['stopped_on_access_limit'])
        self.assertEqual({call[1] for client in clients for call in client.calls}, {'a', 'b'})
        saved = json.loads(path.read_text())['contests']['test']['holdings']
        self.assertEqual(set(saved), {'a'})
        self.assertEqual(saved['a']['status'], 'failed')

    def test_parallel_budget_uses_actual_pending_batches(self):
        self.clock.return_value = '2026-10-12T23:59:57+08:00'
        path = self.checkpoint(['a', 'b', 'c', 'd'])
        checkpoint = json.loads(path.read_text())
        contests = [self.snapshot['contests'][0]]
        with self.assertRaises(collector.SourceError):
            collector.check_holdings_time_budget(self.snapshot, checkpoint, contests, '100', 2)
        collector.check_holdings_time_budget(self.snapshot, checkpoint, contests, '100', 2, workers=2)

    def test_invalid_worker_count_is_rejected_before_requests(self):
        for workers in (0, 3, True):
            with self.subTest(workers=workers), self.assertRaises(collector.SourceError):
                collector.collect_group(self.snapshot, '100', FakeClient(), 2, workers=workers)


class SharedStopTests(unittest.TestCase):
    def setUp(self):
        self.source = collector.PublicClient.__module__

    def test_stopped_worker_never_starts_curl(self):
        stopped = threading.Event()
        stopped.set()
        client = collector.PublicClient(2, stop_event=stopped)
        with mock.patch(self.source + '.subprocess.run') as request:
            with self.assertRaises(collector.RequestsStopped):
                client.request('https://example.test/position')
            request.assert_not_called()

    def test_403_and_429_stop_all_readers_without_retry(self):
        for status in ('403', '429'):
            with self.subTest(status=status):
                stopped = threading.Event()
                client = collector.PublicClient(2, stop_event=stopped)
                response = subprocess.CompletedProcess([], 0, 'blocked\n' + status, '')
                with mock.patch(self.source + '.subprocess.run', return_value=response) as request:
                    with self.assertRaises(collector.AccessLimited):
                        client.request('https://example.test/position')
                    self.assertTrue(stopped.is_set())
                    self.assertEqual(request.call_count, 1)

    def test_retry_is_cancelled_if_other_reader_stops(self):
        stopped = threading.Event()
        client = collector.PublicClient(2, stop_event=stopped)

        def failure(*args, **kwargs):
            stopped.set()
            return subprocess.CompletedProcess([], 0, 'temporary\n500', '')

        with mock.patch(self.source + '.subprocess.run', side_effect=failure) as request:
            with self.assertRaises(collector.RequestsStopped):
                client.request('https://example.test/position')
            self.assertEqual(request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
