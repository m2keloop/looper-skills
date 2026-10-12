"""Offline checks for complete thousand-group public holdings collection."""
import contextlib
import fcntl
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location('collect_thousand_group', SCRIPTS / 'collect_thousand_group.py')
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def member(uid, index, tag='1000'):
    return {'user_key': uid, 'page_index': str(index), 'tag': tag,
            'user_name': '姓名不应复制进归一checkpoint'}


class FakeClient:
    def __init__(self, pages=None, positions=None, failures=None, page_numbers=None):
        self.pages = pages or {}
        self.positions = positions or {}
        self.failures = failures or {}
        self.page_numbers = page_numbers or {}
        self.calls, self.events = [], []

    def request(self, url):
        query = parse_qs(urlparse(url).query, keep_blank_values=True)
        endpoint, key = urlparse(url).path.rsplit('/', 1)[-1], query['key'][0]
        identity = ((endpoint, key, int(query['page'][0])) if endpoint == 'match_ranking_by_share'
                    else (endpoint, key, query['user_key'][0]))
        self.calls.append(identity)
        self.events.append({'endpoint': endpoint, 'key': key})
        if identity in self.failures:
            raise self.failures[identity]
        if endpoint == 'match_ranking_by_share':
            page = identity[2]
            assert query['group_type'] == ['1000']
            data = {'page': self.page_numbers.get((key, page), page),
                    'article_title': '比赛' + key, 'rank_list': self.pages.get((key, page), []),
                    # Deliberately incorrect group totals: they must be ignored.
                    'max_count': 99999, 'max_page': 1, 'count': 50}
        elif endpoint == 'position_by_share':
            data = {'position_list': self.positions.get((key, identity[2]), [])}
        else:
            raise AssertionError(endpoint)
        return {'error_code': '0', 'ex_data': data}


class ThousandGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / 'weights.json'
        self.config_path.write_text(json.dumps({'version': 'public-large-capital-groups-v2',
                                               'weights': {'100': 3, '1000': 5}}), encoding='utf-8')
        self.weights_patch = mock.patch.object(collector, 'WEIGHTS_PATH', self.config_path)
        self.weights_patch.start()
        self.addCleanup(self.weights_patch.stop)
        self.now_patch = mock.patch.object(collector, 'now', return_value='2026-10-08T19:00:00+08:00')
        self.now_patch.start()
        self.addCleanup(self.now_patch.stop)

    def snapshot(self, rows=None, counts=None, threshold=1000, complete=True):
        rows = rows or [('A', '1', '001201', 10), ('A', '2', '600127', 5)]
        counts = counts or {row[0]: 2000 for row in rows}
        path = self.root / ('snapshot-' + str(len(list(self.root.iterdir()))))
        path.mkdir()
        records = [{'contest_key': key, 'competition_name': '比赛' + key,
                    'market': market, 'stock_code': code, 'stock_name': '股票' + code,
                    'holder_count': count, 'stock_rank': index + 1}
                   for index, (key, market, code, count) in enumerate(rows)]
        catalog = [{'contest_key': key, 'competition_name': '比赛' + key,
                    'participant_count': count, 'qualifies': count > threshold,
                    'collection_status': 'collected' if count > threshold else 'excluded',
                    'returned_stock_count': sum(row['contest_key'] == key for row in records)}
                   for key, count in counts.items()]
        selected = sum(item['qualifies'] for item in catalog)
        manifest = {'run_id': path.name, 'completed_at': '2026-10-08T18:30:00+08:00',
                    'collection_complete': complete, 'scope': '测试目录',
                    'threshold': threshold, 'stock_scope': '官方热榜',
                    'selection_rule': 'participant_count > threshold',
                    'failed_contest_count': 0, 'pending_contest_count': 0,
                    'hot_contest_count': selected, 'collected_contest_count': selected,
                    'stock_row_count': len(records), 'catalog_entry_count': len(catalog)}
        for filename, value in (
                ('manifest.json', manifest),
                ('catalog.json', {'scope': manifest['scope'], 'threshold': threshold, 'items': catalog}),
                ('all-hot-stocks.json', {'metadata': manifest, 'records': records})):
            (path / filename).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def collect(self, path, client):
        with contextlib.redirect_stdout(io.StringIO()):
            status = collector.run_collection(Namespace(snapshot=path, interval=2), client)
        payload = json.loads((path / 'thousand-group.json').read_text())
        return status, payload

    def test_full_roster_to_empty_page_and_deduplicated_public_holdings(self):
        path = self.snapshot()
        client = FakeClient(
            pages={('A', 1): [member('p1', 1), member('p2', 2)],
                   ('A', 2): [member('p3', 3)]},
            positions={('A', 'p1'): [{'stock_code': '001201'}, {'stock_code': '001201'},
                                    {'stock_code': 'not-in-current-hot-list'}],
                       ('A', 'p2'): [], ('A', 'p3'): [{'stock_code': '600127'}]})
        status, payload = self.collect(path, client)
        self.assertEqual(status, 0)
        self.assertTrue(payload['metadata']['collection_complete'])
        self.assertEqual(payload['metadata']['weights']['1000'], 5)
        self.assertEqual(set(payload['metadata']['weights']), {'100', '1000'})
        self.assertEqual(payload['metadata']['base_run_id'], path.name)
        self.assertEqual(payload['contests'][0]['member_count'], 3)
        self.assertEqual(payload['contests'][0]['public_empty_list_count'], 1)
        self.assertEqual({r['stock_code']: r['thousand_holder_count'] for r in payload['records']},
                         {'001201': 1, '600127': 1})
        self.assertEqual([call[2] for call in client.calls if call[0] == 'match_ranking_by_share'],
                         [1, 2, 3])
        self.assertEqual([call[0] for call in client.calls[:3]], ['match_ranking_by_share'] * 3)
        checkpoint = (path / 'thousand-group-checkpoint.json').read_text()
        self.assertNotIn('user_name', checkpoint)
        self.assertNotIn('姓名', checkpoint)
        self.assertTrue(list((path / 'competitions/A/raw/thousand-group/positions').glob('*.json')))

    def test_empty_group_is_complete_and_every_base_stock_gets_known_zero(self):
        path = self.snapshot()
        status, payload = self.collect(path, FakeClient())
        self.assertEqual(status, 0)
        self.assertEqual(payload['contests'][0]['member_count'], 0)
        self.assertEqual(len(payload['records']), 2)
        self.assertEqual([r['thousand_holder_count'] for r in payload['records']], [0, 0])

    def test_wrong_tags_duplicate_members_pages_or_indices_fail_closed(self):
        cases = [
            ({('A', 1): [member('p1', 1, tag='100')]}, {}),
            ({('A', 1): [member('p1', 1), member('p1', 2)]}, {}),
            ({('A', 1): [member('p1', 2)]}, {}),
            ({('A', 1): [member('p1', 1)], ('A', 2): [member('p1', 1)]}, {}),
            ({('A', 1): [member('p1', 1)]}, {('A', 1): 2}),
            ({('A', 1): [member('p1', 1)]}, {('A', 1): 1.5}),
        ]
        for pages, page_numbers in cases:
            with self.subTest(pages=pages, page_numbers=page_numbers):
                path = self.snapshot()
                client = FakeClient(pages=pages, page_numbers=page_numbers)
                status, payload = self.collect(path, client)
                self.assertEqual(status, 2)
                self.assertFalse(payload['metadata']['collection_complete'])
                self.assertTrue(all(r['thousand_holder_count'] is None for r in payload['records']))
                self.assertFalse(any(call[0] == 'position_by_share' for call in client.calls))

    def test_group_count_cannot_exceed_whole_contest_safety_bound(self):
        path = self.snapshot(counts={'A': 2}, threshold=0)
        client = FakeClient(pages={('A', 1): [member('p1', 1), member('p2', 2), member('p3', 3)]})
        status, payload = self.collect(path, client)
        self.assertEqual(status, 2)
        self.assertIn('exceeds', payload['contests'][0]['error'])

    def test_access_limits_stop_all_following_requests_and_leave_unknowns(self):
        for http in ('403', '429'):
            with self.subTest(http=http):
                path = self.snapshot(rows=[('A', '1', '001201', 10), ('B', '2', '600127', 10)])
                client = FakeClient(
                    pages={('A', 1): [member('p1', 1)], ('B', 1): [member('p2', 1)]},
                    failures={('position_by_share', 'A', 'p1'): collector.AccessLimited('HTTP ' + http)})
                status, payload = self.collect(path, client)
                self.assertEqual(status, 2)
                self.assertTrue(payload['metadata']['stopped_on_access_limit'])
                self.assertEqual(client.calls[-1], ('position_by_share', 'A', 'p1'))
                self.assertNotIn(('position_by_share', 'B', 'p2'), client.calls)
                self.assertTrue(all(r['thousand_holder_count'] is None for r in payload['records']))

    def test_rank_access_limit_stops_before_next_contest_or_holdings(self):
        path = self.snapshot(rows=[('A', '1', '001201', 10), ('B', '2', '600127', 10)])
        client = FakeClient(failures={('match_ranking_by_share', 'A', 1):
                                     collector.AccessLimited('HTTP 429')})
        status, payload = self.collect(path, client)
        self.assertEqual(status, 2)
        self.assertEqual(client.calls, [('match_ranking_by_share', 'A', 1)])
        self.assertTrue(payload['metadata']['stopped_on_access_limit'])
        self.assertTrue(all(r['thousand_holder_count'] is None for r in payload['records']))

    def test_missing_public_position_list_stays_unknown_and_weights_fail_early(self):
        path = self.snapshot()
        client = FakeClient(pages={('A', 1): [member('p1', 1)]},
                            positions={('A', 'p1'): None})
        status, payload = self.collect(path, client)
        self.assertEqual(status, 2)
        self.assertTrue(all(r['thousand_holder_count'] is None for r in payload['records']))
        config_path = self.root / 'weights.json'
        for weights in ({'1': 1, '10': 2, '100': 3, '1000': 5},
                        {'100': 1, '1000': 1},
                        {'100': 1, '1000': float('inf')},
                        {'100': 0, '1000': 5},
                        {'100': -1, '1000': 5},
                        {'100': 6, '1000': 5},
                        {'100': True, '1000': 5},
                        {'1000': 5}):
            with self.subTest(weights=weights):
                config_path.write_text(json.dumps({'version': 'bad', 'weights': weights}))
                with mock.patch.object(collector, 'WEIGHTS_PATH', config_path):
                    with self.assertRaises(collector.SourceError):
                        collector.weight_config()

    def test_two_positive_distinct_weights_accept_thousand_as_highest(self):
        config = collector.weight_config()
        self.assertEqual(config, {'weight_version': 'public-large-capital-groups-v2',
                                  'weights': {'100': 3, '1000': 5}})

    def test_failed_member_keeps_whole_contest_unknown_and_resume_reuses_only_successes(self):
        path = self.snapshot()
        first = FakeClient(
            pages={('A', 1): [member('p1', 1), member('p2', 2)]},
            positions={('A', 'p1'): [{'stock_code': '001201'}]},
            failures={('position_by_share', 'A', 'p2'): collector.SourceError('Source failure')})
        status, payload = self.collect(path, first)
        self.assertEqual(status, 2)
        self.assertEqual(payload['contests'][0]['holdings_success_count'], 1)
        self.assertEqual(payload['contests'][0]['holdings_failed_count'], 1)
        self.assertTrue(all(r['thousand_holder_count'] is None for r in payload['records']))
        second = FakeClient(positions={('A', 'p2'): [{'stock_code': '001201'}]})
        status, payload = self.collect(path, second)
        self.assertEqual(status, 0)
        self.assertEqual(second.calls, [('position_by_share', 'A', 'p2')])
        self.assertEqual(payload['records'][0]['thousand_holder_count'], 2)
        third = FakeClient()
        self.assertEqual(self.collect(path, third)[0], 0)
        self.assertEqual(third.calls, [])

    def test_other_day_checkpoint_is_not_reused(self):
        path = self.snapshot()
        first = FakeClient(pages={('A', 1): [member('p1', 1)]})
        self.assertEqual(self.collect(path, first)[0], 0)
        checkpoint_path = path / 'thousand-group-checkpoint.json'
        checkpoint = json.loads(checkpoint_path.read_text())
        checkpoint['metadata']['collection_date'] = '2026-10-07'
        checkpoint_path.write_text(json.dumps(checkpoint))
        second = FakeClient(pages={('A', 1): [member('p1', 1)]})
        self.assertEqual(self.collect(path, second)[0], 0)
        self.assertEqual(len(second.calls), 3)

    def test_ambiguous_market_mapping_is_rejected_without_network_guessing(self):
        path = self.snapshot(rows=[('A', '1', '001201', 10), ('A', '2', '001201', 10)])
        client = FakeClient()
        status, payload = self.collect(path, client)
        self.assertEqual(status, 2)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(payload['records']), 2)
        self.assertTrue(all(r['thousand_holder_count'] is None for r in payload['records']))
        self.assertIn('multiple markets', payload['contests'][0]['error'])

    def test_mixed_source_count_excess_is_flagged_without_inferred_other_groups(self):
        path = self.snapshot(rows=[('A', '1', '001201', 0)])
        client = FakeClient(pages={('A', 1): [member('p1', 1)]},
                            positions={('A', 'p1'): [{'stock_code': '001201'}]})
        status, payload = self.collect(path, client)
        self.assertEqual(status, 0)
        self.assertTrue(payload['records'][0]['count_exceeds_official'])
        self.assertTrue(payload['metadata']['warnings'])
        self.assertNotIn('other_holder_count', payload['records'][0])

    def test_invalid_base_snapshot_and_overlap_are_rejected(self):
        incomplete = self.snapshot(complete=False)
        with self.assertRaises(ValueError):
            self.collect(incomplete, FakeClient())
        path = self.snapshot()
        with (path / '.thousand-group.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(sys, 'argv', ['collect_thousand_group.py', '--snapshot', str(path)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(collector.main(), 3)
        self.assertFalse((path / 'thousand-group.json').exists())


if __name__ == '__main__':
    unittest.main()
