"""Offline behavior checks for the public hot-contest snapshot workflow."""
import contextlib
import csv
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'collect_hot_contests.py'
SPEC = importlib.util.spec_from_file_location('collect_hot_contests', SCRIPT)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)
SOURCE_TIME = datetime(2026, 10, 8, 17, 5, tzinfo=ZoneInfo('Asia/Shanghai'))
SOURCE_TIME_MS = int(SOURCE_TIME.timestamp() * 1000)


def official_data(data):
    return {'error_code': '0', 'ex_data': data}


def contest(key, count, directory_count=None):
    return {'key': key, 'title': '比赛 ' + key, 'count': count,
            'directory_count': count if directory_count is None else directory_count}


class FakeClient:
    """Fake the source boundary, retaining the exact outbound request sequence."""
    def __init__(self, contests, directory_error=None, failures=None, holder_counts=None):
        self.contests = {item['key']: item for item in contests}
        self.directory_error = directory_error
        self.failures = failures or {}
        self.holder_counts = holder_counts or {}
        self.calls = []
        self.events = []

    def request(self, url):
        self.calls.append(url)
        self.events.append({'url': url})
        if url == collector.DIRECTORY_URL:
            if self.directory_error:
                raise self.directory_error
            return {'code': 0, 'data': {'items': [
                {'key': item['key'], 'title': item['title'],
                 'participants': item['directory_count']}
                for item in self.contests.values()
            ]}}
        parsed = urlparse(url)
        endpoint = parsed.path.rsplit('/', 1)[-1]
        key = parse_qs(parsed.query)['key'][0]
        if (key, endpoint) in self.failures:
            raise self.failures[(key, endpoint)]
        item = self.contests[key]
        if endpoint == 'match_ranking_by_share':
            return official_data({'article_title': item['title'],
                                  'max_count': str(item['count']), 'status': 0,
                                  'match_start': '20260101', 'match_end': '20261231'})
        if endpoint == 'match_stock_ranking_by_share':
            return official_data({'match_name': item['title'], 'time': SOURCE_TIME_MS,
                                  'hot_stock_list': [
                                      {'stock_code': '001201', 'stock_name': '东瑞股份',
                                       'market': '33', 'hold_num': str(self.holder_counts.get(key, 7))}
                                  ]})
        raise AssertionError('Unexpected source request: ' + url)

    def requests_for(self, endpoint):
        return [parse_qs(urlparse(url).query)['key'][0]
                for url in self.calls if urlparse(url).path.endswith('/' + endpoint)]


class HotContestCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def collect(self, contests, client=None, preserve_latest=False):
        registry = self.root / 'registry.json'
        registry.write_text(json.dumps({'items': [
            {'key': item['key'], 'title': item['title']} for item in contests
        ]}), encoding='utf-8')
        output = self.root / 'snapshots'
        output.mkdir(exist_ok=True)
        previous_latest = {'run_id': 'previous-complete', 'relative_path': 'previous-complete'}
        if preserve_latest:
            (output / 'latest.json').write_text(json.dumps(previous_latest), encoding='utf-8')
        args = Namespace(registry=registry, output=output, threshold=1000, interval=2.0)
        client = client or FakeClient(contests)
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = collector.run_collection(args, client=client)
        runs = [path for path in output.iterdir() if path.is_dir()]
        self.assertEqual(len(runs), 1)
        run = runs[0]
        manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
        catalog = json.loads((run / 'catalog.json').read_text(encoding='utf-8'))['items']
        records = json.loads((run / 'all-hot-stocks.json').read_text(encoding='utf-8'))['records']
        return exit_code, run, manifest, catalog, records, output, previous_latest

    def test_strict_threshold_uses_latest_official_counts_despite_stale_directory(self):
        contests = [contest('Below999', 999, 9999), contest('Equal1000', 1000, 9999),
                    contest('Above1001', 1001, 1)]
        client = FakeClient(contests)
        result = self.collect(contests, client)
        exit_code, run, manifest, catalog, records, output, _ = result
        self.assertEqual(exit_code, 0)
        self.assertTrue(manifest['collection_complete'])
        self.assertEqual(client.requests_for('match_ranking_by_share'),
                         ['Below999', 'Equal1000', 'Above1001'])
        self.assertEqual(client.requests_for('match_stock_ranking_by_share'), ['Above1001'])
        self.assertEqual({item['contest_key']: item['collection_status'] for item in catalog},
                         {'Below999': 'excluded', 'Equal1000': 'excluded', 'Above1001': 'collected'})
        self.assertEqual([(row['contest_key'], row['participant_count']) for row in records],
                         [('Above1001', 1001)])
        self.assertEqual(manifest['hot_contest_count'], 1)
        self.assertEqual(json.loads((output / 'latest.json').read_text())['run_id'], run.name)

    def test_same_stock_keeps_contest_counts_separate_without_inventing_unique_holders(self):
        contests = [contest('MatchAAA', 2000), contest('MatchBBB', 3000)]
        client = FakeClient(contests, holder_counts={'MatchAAA': 12, 'MatchBBB': 9})
        _, _, manifest, _, records, _, _ = self.collect(contests, client)
        self.assertEqual(len(records), 2)
        self.assertEqual({row['contest_key']: row['holder_count'] for row in records},
                         {'MatchAAA': 12, 'MatchBBB': 9})
        self.assertEqual({row['stock_code'] for row in records}, {'001201'})
        self.assertEqual(manifest['unique_returned_stock_count'], 1)
        self.assertNotIn('unique_holder_count', manifest)
        self.assertIn('不能', manifest['cross_contest_note'])

    def test_access_limit_stops_globally_and_preserves_previous_complete_snapshot(self):
        for status in (403, 429):
            with self.subTest(status=status):
                # Each subcase gets a separate output directory and registry.
                self.root = Path(self.temp.name) / str(status)
                self.root.mkdir()
                contests = [contest('FirstAAA', 2000), contest('SecondBB', 3000),
                            contest('ThirdCCC', 4000)]
                limited = collector.AccessLimited(f'HTTP {status}: stopped')
                client = FakeClient(contests, failures={
                    ('SecondBB', 'match_stock_ranking_by_share'): limited
                })
                result = self.collect(contests, client, preserve_latest=True)
                exit_code, _, manifest, catalog, records, output, previous = result
                self.assertEqual(exit_code, 2)
                self.assertFalse(manifest['collection_complete'])
                self.assertTrue(manifest['stopped_on_access_limit'])
                self.assertEqual(manifest['collected_contest_count'], 1)
                self.assertEqual(manifest['failed_contest_count'], 1)
                self.assertEqual(manifest['pending_contest_count'], 1)
                self.assertEqual(client.requests_for('match_ranking_by_share'), ['FirstAAA', 'SecondBB'])
                self.assertEqual(client.requests_for('match_stock_ranking_by_share'), ['FirstAAA', 'SecondBB'])
                self.assertEqual({row['contest_key'] for row in records}, {'FirstAAA'})
                by_key = {entry['contest_key']: entry for entry in catalog}
                self.assertEqual(by_key['SecondBB']['failed_stage'], 'hot_stocks')
                self.assertEqual(by_key['ThirdCCC']['collection_status'], 'pending')
                self.assertEqual(json.loads((output / 'latest.json').read_text()), previous)

    def test_directory_access_limit_makes_no_official_requests(self):
        contests = [contest('MatchAAA', 2000)]
        client = FakeClient(contests, directory_error=collector.AccessLimited('HTTP 429: stopped'))
        exit_code, _, manifest, _, records, output, previous = self.collect(
            contests, client, preserve_latest=True)
        self.assertEqual(exit_code, 2)
        self.assertEqual(client.calls, [collector.DIRECTORY_URL])
        self.assertEqual(manifest['participant_counts_checked'], 0)
        self.assertEqual(manifest['pending_contest_count'], 1)
        self.assertTrue(manifest['stopped_on_access_limit'])
        self.assertFalse(manifest['directory_refreshed'])
        self.assertEqual(records, [])
        self.assertEqual(json.loads((output / 'latest.json').read_text()), previous)

    def test_directory_failure_collects_registry_without_claiming_complete_discovery(self):
        contests = [contest('MatchAAA', 2000)]
        client = FakeClient(contests, directory_error=collector.SourceError('HTTP 503 unavailable'))
        exit_code, _, manifest, catalog, records, output, previous = self.collect(
            contests, client, preserve_latest=True)
        self.assertEqual(exit_code, 2)
        self.assertFalse(manifest['directory_refreshed'])
        self.assertFalse(manifest['collection_complete'])
        self.assertFalse(manifest['stopped_on_access_limit'])
        self.assertEqual(manifest['collected_contest_count'], 1)
        self.assertEqual(catalog[0]['collection_status'], 'collected')
        self.assertEqual(records[0]['contest_key'], 'MatchAAA')
        self.assertIn('using saved registry', '\n'.join(manifest['warnings']))
        self.assertEqual(json.loads((output / 'latest.json').read_text()), previous)

    def test_leading_zero_code_source_timestamp_and_raw_evidence_are_retained(self):
        contests = [contest('MatchAAA', 2000)]
        _, run, _, _, records, _, _ = self.collect(contests)
        self.assertEqual(records[0]['stock_code'], '001201')
        self.assertEqual(records[0]['source_updated_at'], '2026-10-08T17:05:00+08:00')
        self.assertIsNotNone(records[0]['fetched_at'])
        hot = json.loads((run / 'competitions' / 'MatchAAA' / 'hot-stocks.json').read_text())
        self.assertEqual(hot['metadata']['source_updated_at'], '2026-10-08T17:05:00+08:00')
        raw = json.loads((run / 'competitions' / 'MatchAAA' / 'raw' /
                          'hot-stock-response.json').read_text())
        self.assertEqual(raw['ex_data']['time'], SOURCE_TIME_MS)
        self.assertEqual(raw['ex_data']['hot_stock_list'][0]['stock_code'], '001201')
        with (run / 'all-hot-stocks.csv').open(encoding='utf-8-sig', newline='') as file:
            csv_row = next(csv.DictReader(file))
        self.assertEqual(csv_row['stock_code'], '001201')
        self.assertEqual(csv_row['source_updated_at'], '2026-10-08T17:05:00+08:00')

    def test_public_client_does_not_retry_http_429(self):
        response = subprocess.CompletedProcess(['curl'], 0, stdout='Too many requests\n429', stderr='')
        client = collector.PublicClient(interval=2.0)
        with mock.patch.object(collector.subprocess, 'run', return_value=response) as request, \
             mock.patch.object(collector.time, 'sleep'):
            with self.assertRaises(collector.AccessLimited):
                client.request('https://example.test/public')
        request.assert_called_once()
        self.assertEqual(len(client.events), 1)
        self.assertEqual(client.events[0]['http_status'], '429')


if __name__ == '__main__':
    unittest.main()
