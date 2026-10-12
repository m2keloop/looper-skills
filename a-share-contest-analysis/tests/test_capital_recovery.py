"""Offline regressions for capital-tier recovery and snapshot identity gates."""
import contextlib
import copy
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
SPEC = importlib.util.spec_from_file_location('capital_recovery_collector', SCRIPTS / 'collect_capital_groups.py')
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def member(uid, index, code='100'):
    return {'user_key': uid, 'page_index': str(index), 'tag': code}


class FakeClient:
    def __init__(self, pages=None, failures=None, malformed=None, on_position=None):
        self.pages = pages or {}
        self.failures = failures or {}
        self.malformed = malformed or {}
        self.on_position = on_position
        self.calls, self.events = [], []

    def request(self, url):
        query = parse_qs(urlparse(url).query, keep_blank_values=True)
        endpoint = urlparse(url).path.rsplit('/', 1)[-1]
        if endpoint == 'match_ranking_by_share':
            code, page = query['group_type'][0], int(query['page'][0])
            identity = (endpoint, code, page)
            data = {'page': page, 'article_title': '测试比赛',
                    'rank_list': self.pages.get((code, page), [])}
            data.update(self.malformed.get((code, page), {}))
        else:
            self.assert_endpoint(endpoint)
            identity = (endpoint, query['user_key'][0])
            data = {'position_list': [{'stock_code': '600127'}]}
            if self.on_position:
                self.on_position()
        self.calls.append(identity)
        self.events.append({'endpoint': endpoint})
        if identity in self.failures:
            raise self.failures[identity]
        return {'error_code': '0', 'ex_data': data}

    @staticmethod
    def assert_endpoint(endpoint):
        if endpoint != 'position_by_share':
            raise AssertionError(endpoint)


class CapitalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test-snapshot'
        self.path.mkdir()
        weights_path = Path(self.tmp.name) / 'weights.json'
        weights_path.write_text(json.dumps({'version': 'public-large-capital-groups-v2',
                                           'weights': {'100': 3, '1000': 5}}), encoding='utf-8')
        mock.patch.object(collector, 'WEIGHTS_PATH', weights_path).start()
        self.clock = mock.patch.object(collector, 'now', return_value='2026-10-12T16:40:00+08:00').start()
        self.addCleanup(mock.patch.stopall)
        manifest = {'run_id': self.path.name, 'completed_at': '2026-10-12T16:30:00+08:00',
                    'collection_complete': True, 'scope': '测试目录', 'threshold': 1000,
                    'stock_scope': '官方热榜', 'selection_rule': 'participant_count > threshold',
                    'failed_contest_count': 0, 'pending_contest_count': 0,
                    'hot_contest_count': 1, 'collected_contest_count': 1,
                    'stock_row_count': 1, 'catalog_entry_count': 1}
        contest = {'contest_key': 'test', 'competition_name': '测试比赛', 'participant_count': 2000,
                   'qualifies': True, 'collection_status': 'collected', 'returned_stock_count': 1}
        row = {'contest_key': 'test', 'competition_name': '测试比赛', 'market': '2',
               'stock_code': '600127', 'stock_name': '测试股票', 'holder_count': 4, 'stock_rank': 1}
        for name, data in (('manifest.json', manifest),
                           ('catalog.json', {'scope': manifest['scope'], 'threshold': 1000,
                                             'items': [contest]}),
                           ('all-hot-stocks.json', {'metadata': manifest, 'records': [row]})):
            collector.save(self.path / name, data)
        self.snapshot = collector.read_snapshot(self.path)

    def collect(self, client, code='100'):
        with contextlib.redirect_stdout(io.StringIO()):
            return collector.collect_group(self.snapshot, code, client, 2)

    def thousand(self):
        payload = {'metadata': {**collector.snapshot_identity(self.snapshot),
                                'group_code': '1000', 'collection_complete': True,
                                'completed_at': '2026-10-12T16:35:00+08:00',
                                'member_count': 1, 'holdings_success_count': 1,
                                'holdings_failed_count': 0, 'scope': '测试公开持仓'},
                   'contests': [{'contest_key': 'test', 'member_count': 1,
                                 'holdings_success_count': 1, 'holdings_failed_count': 0,
                                 'ranking_complete': True, 'collection_complete': True}],
                   'records': [{**self.snapshot['rows'][0], 'thousand_holder_count': 1,
                                'collection_complete': True}]}
        collector.save(self.path / 'thousand-group.json', payload)
        return payload

    def test_retry_resumes_ranking_and_clears_error_only_after_valid_terminal_page(self):
        initial = FakeClient(pages={('100', 1): [member('first', 1)]},
                             failures={('match_ranking_by_share', '100', 2): collector.SourceError('temporary ranking failure')})
        first = self.collect(initial)
        self.assertFalse(first['metadata']['collection_complete'])
        checkpoint_path = self.path / 'group-100-checkpoint.json'
        entry = json.loads(checkpoint_path.read_text())['contests']['test']
        self.assertEqual(entry['failed_stage'], 'ranking')
        self.assertEqual(initial.calls, [('match_ranking_by_share', '100', 1),
                                         ('match_ranking_by_share', '100', 2)])
        resumed = FakeClient(pages={('100', 2): [member('second', 2)]})
        complete = self.collect(resumed)
        entry = json.loads(checkpoint_path.read_text())['contests']['test']
        self.assertNotIn('failed_stage', entry)
        self.assertNotIn('error', entry)
        self.assertEqual(resumed.calls[:2], [('match_ranking_by_share', '100', 2),
                                            ('match_ranking_by_share', '100', 3)])
        self.assertEqual(resumed.calls[2:], [('position_by_share', 'first'), ('position_by_share', 'second')])
        self.assertTrue(complete['metadata']['collection_complete'])
        self.assertEqual(complete['records'][0]['group_holder_count'], 2)
        for field in collector.IDENTITY_FIELDS:
            self.assertEqual(complete['metadata'][field], collector.snapshot_identity(self.snapshot)[field])

    def test_failed_ranking_is_not_cleared_by_another_invalid_response(self):
        self.collect(FakeClient(failures={('match_ranking_by_share', '100', 1): collector.SourceError('first failure')}))
        invalid = FakeClient(malformed={('100', 1): {'page': 999}})
        result = self.collect(invalid)
        entry = json.loads((self.path / 'group-100-checkpoint.json').read_text())['contests']['test']
        self.assertFalse(result['metadata']['collection_complete'])
        self.assertEqual(entry['failed_stage'], 'ranking')
        self.assertFalse(entry['ranking_complete'])
        self.assertIn('another page', entry['error'])
        self.assertEqual(len(invalid.calls), 1)

    def test_legacy_complete_flag_does_not_bypass_unresolved_ranking_error(self):
        path, checkpoint = collector.load_checkpoint(self.snapshot, '100')
        checkpoint['contests']['test'] = {'members': ['first'], 'holdings': {}, 'last_page': 1,
                                         'ranking_complete': True, 'ranking_status': 'failed',
                                         'failed_stage': 'ranking', 'error': 'old failure'}
        collector.save(path, checkpoint)
        client = FakeClient()
        result = self.collect(client)
        self.assertEqual(client.calls, [('match_ranking_by_share', '100', 2), ('position_by_share', 'first')])
        self.assertTrue(result['metadata']['collection_complete'])

    def test_access_limit_stops_without_holdings_or_losing_checkpoint(self):
        client = FakeClient(pages={('100', 1): [member('first', 1)]},
                            failures={('match_ranking_by_share', '100', 2): collector.AccessLimited('HTTP 429')})
        result = self.collect(client)
        self.assertTrue(result['metadata']['stopped_on_access_limit'])
        self.assertFalse(result['metadata']['collection_complete'])
        self.assertTrue((self.path / 'group-100-checkpoint.json').exists())
        self.assertEqual(len(client.calls), 2)

    def test_thousand_reuse_preserves_all_identity_and_completion_fields(self):
        source = self.thousand()
        reused = collector.read_existing_thousand(self.snapshot)
        for field in (*collector.IDENTITY_FIELDS, 'completed_at'):
            self.assertEqual(reused['metadata'][field], source['metadata'][field])
        self.assertEqual(reused['records'][0]['group_holder_count'], 1)

    def test_historic_four_tier_stamp_is_not_adopted_or_rewritten(self):
        source = self.thousand()
        source['metadata'].update(weight_version='public-capital-groups-v1',
                                  weights={'1': 1, '10': 2, '100': 3, '1000': 5})
        path = self.path / 'thousand-group.json'
        collector.save(path, source)
        original = path.read_bytes()
        reused = collector.read_existing_thousand(self.snapshot)
        self.assertNotIn('weights', reused['metadata'])
        self.assertNotIn('weight_version', reused['metadata'])
        self.assertEqual(reused['records'][0]['group_holder_count'], 1)
        self.assertEqual(path.read_bytes(), original)

    def test_disabled_groups_are_rejected_without_checkpoint_or_requests(self):
        for code in ('1', '10'):
            with self.subTest(code=code):
                path = self.path / f'group-{code}-checkpoint.json'
                # Malformed inactive files must never even be parsed.
                path.write_text('historical checkpoint retained', encoding='utf-8')
                client = FakeClient()
                with self.assertRaisesRegex(collector.SourceError, 'Only million/thousand'):
                    self.collect(client, code)
                self.assertEqual(client.calls, [])
                self.assertEqual(path.read_text(), 'historical checkpoint retained')

    def test_utc_z_completion_is_compared_in_shanghai_timezone(self):
        source = self.thousand()
        source['metadata']['completed_at'] = '2026-10-12T08:35:00Z'
        collector.save(self.path / 'thousand-group.json', source)
        reused = collector.read_existing_thousand(self.snapshot)
        self.assertEqual(reused['metadata']['completed_at'], '2026-10-12T08:35:00Z')

    def test_thousand_rejects_stale_or_missing_identity_and_cross_day_completion(self):
        source = self.thousand()
        invalid_values = {'schema_version': True, 'base_run_id': 'other',
                          'base_completed_at': '2026-10-12T16:29:00+08:00',
                          'base_fingerprint': 'changed', 'collection_date': '2026-10-11',
                          'completed_at': '2026-10-13T00:01:00+08:00'}
        for field, value in invalid_values.items():
            with self.subTest(field=field):
                payload = copy.deepcopy(source)
                payload['metadata'][field] = value
                collector.save(self.path / 'thousand-group.json', payload)
                with self.assertRaises(collector.SourceError):
                    collector.read_existing_thousand(self.snapshot)
        for field in (*collector.IDENTITY_FIELDS, 'completed_at'):
            with self.subTest(missing=field):
                payload = copy.deepcopy(source)
                payload['metadata'].pop(field)
                collector.save(self.path / 'thousand-group.json', payload)
                with self.assertRaises(collector.SourceError):
                    collector.read_existing_thousand(self.snapshot)

    def test_base_file_mutation_invalidates_existing_thousand_collection(self):
        self.thousand()
        with (self.path / 'catalog.json').open('a') as output:
            output.write('\n')
        with self.assertRaises(collector.SourceError):
            collector.read_existing_thousand(self.snapshot)

    def test_next_day_reuse_rejects_before_client_creation_and_keeps_checkpoint(self):
        self.thousand()
        checkpoint = self.path / 'group-100-checkpoint.json'
        checkpoint.write_text('{"retained": true}', encoding='utf-8')
        self.clock.return_value = '2026-10-13T16:40:00+08:00'
        with mock.patch.object(collector, 'PublicClient') as client:
            with self.assertRaisesRegex(collector.SourceError, 'from today'):
                collector.run(Namespace(snapshot=self.path, interval=2))
        client.assert_not_called()
        self.assertEqual(checkpoint.read_text(), '{"retained": true}')
        self.assertFalse((self.path / 'group-holdings.json').exists())

    def test_collection_crossing_midnight_rejects_and_retains_successful_progress(self):
        def midnight():
            self.clock.return_value = '2026-10-13T00:01:00+08:00'
        client = FakeClient(pages={('100', 1): [member('first', 1)]}, on_position=midnight)
        with self.assertRaises(collector.SourceError):
            self.collect(client)
        checkpoint = json.loads((self.path / 'group-100-checkpoint.json').read_text())
        self.assertEqual(checkpoint['metadata']['collection_date'], '2026-10-12')
        self.assertEqual(checkpoint['contests']['test']['holdings']['first']['status'], 'success')
        self.assertFalse((self.path / 'group-holdings.json').exists())

    def test_sufficient_same_day_budget_continues_into_all_pending_holdings(self):
        self.clock.return_value = '2026-10-12T23:59:50+08:00'
        client = FakeClient(pages={('100', 1): [member('first', 1), member('second', 2), member('third', 3)]})
        result = self.collect(client)
        self.assertTrue(result['metadata']['collection_complete'])
        self.assertEqual(client.calls[2:], [('position_by_share', 'first'),
                                          ('position_by_share', 'second'), ('position_by_share', 'third')])

    def test_request_interval_lower_bound_equal_to_remaining_day_stops_before_holdings(self):
        self.clock.return_value = '2026-10-12T23:59:56+08:00'
        client = FakeClient(pages={('100', 1): [member('first', 1), member('second', 2), member('third', 3)]})
        with self.assertRaisesRegex(collector.SourceError, '至少需要 4 秒.*当天剩余 4 秒.*保留检查点'):
            self.collect(client)
        self.assertEqual(client.calls, [('match_ranking_by_share', '100', 1),
                                       ('match_ranking_by_share', '100', 2)])
        saved = json.loads((self.path / 'group-100-checkpoint.json').read_text())
        entry = saved['contests']['test']
        self.assertTrue(entry['ranking_complete'])
        self.assertEqual(entry['members'], ['first', 'second', 'third'])
        self.assertEqual(entry['holdings'], {})
        self.assertEqual(len(saved['request_log']), 2)
        self.assertFalse((self.path / 'group-holdings.json').exists())

    def test_same_day_successful_checkpoint_reduces_actual_pending_budget(self):
        self.clock.return_value = '2026-10-12T23:59:57+08:00'
        path, checkpoint = collector.load_checkpoint(self.snapshot, '100')
        checkpoint['contests']['test'] = {
            'members': ['first', 'second', 'third', 'fourth'], 'last_page': 1,
            'ranking_complete': True, 'ranking_status': 'complete',
            'holdings': {'first': {'status': 'success', 'stock_codes': ['600127']},
                         'second': {'status': 'success', 'stock_codes': []},
                         'third': {'status': 'failed', 'error': 'temporary failure'}}}
        collector.save(path, checkpoint)
        client = FakeClient()
        result = self.collect(client)
        self.assertTrue(result['metadata']['collection_complete'])
        self.assertEqual(client.calls, [('position_by_share', 'third'), ('position_by_share', 'fourth')])

    def test_valid_merge_preserves_identity_then_cleans_checkpoints(self):
        source = self.thousand()
        source['metadata'].update(weight_version='public-capital-groups-v1',
                                  weights={'1': 1, '10': 2, '100': 3, '1000': 5})
        source_path = self.path / 'thousand-group.json'
        collector.save(source_path, source)
        original = source_path.read_bytes()
        (self.path / 'thousand-group-checkpoint.json').write_text('{}', encoding='utf-8')
        client = FakeClient(pages={(code, 1): [member('member-' + code, 1, code)] for code in ('100',)})
        with mock.patch.object(collector, 'PublicClient', return_value=client), contextlib.redirect_stdout(io.StringIO()):
            collector.run(Namespace(snapshot=self.path, interval=2))
        result = json.loads((self.path / 'group-holdings.json').read_text())
        self.assertTrue(result['metadata']['collection_complete'])
        for field in collector.IDENTITY_FIELDS:
            self.assertEqual(result['metadata'][field], source['metadata'][field])
            for group in result['groups'].values():
                self.assertEqual(group['metadata'][field], source['metadata'][field])
        self.assertEqual(result['metadata']['member_count'], 2)
        self.assertEqual(set(result['groups']), {'100', '1000'})
        self.assertEqual(result['metadata']['weights'], {'100': 3, '1000': 5})
        self.assertEqual(result['metadata']['weight_version'], 'public-large-capital-groups-v2')
        self.assertNotIn('weights', result['groups']['1000']['metadata'])
        self.assertNotIn('weight_version', result['groups']['1000']['metadata'])
        self.assertEqual(source_path.read_bytes(), original)
        self.assertEqual([call for call in client.calls if call[0] == 'match_ranking_by_share'],
                         [('match_ranking_by_share', '100', 1), ('match_ranking_by_share', '100', 2)])
        self.assertFalse(any(self.path.glob('*checkpoint.json')))

    def test_valid_merge_never_reads_or_removes_disabled_group_artifacts(self):
        self.thousand()
        inactive = []
        for code in ('1', '10'):
            checkpoint = self.path / f'group-{code}-checkpoint.json'
            checkpoint.write_text('invalid historical data, deliberately unparsed', encoding='utf-8')
            raw = self.path / 'competitions' / 'test' / 'raw' / f'group-{code}' / 'held.json'
            raw.parent.mkdir(parents=True)
            raw.write_text('retained raw data', encoding='utf-8')
            inactive.extend((checkpoint, raw))
        originals = {path: path.read_bytes() for path in inactive}
        client = FakeClient(pages={('100', 1): [member('million', 1)]})
        with mock.patch.object(collector, 'PublicClient', return_value=client), contextlib.redirect_stdout(io.StringIO()):
            collector.run(Namespace(snapshot=self.path, interval=2))
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse((self.path / 'group-100-checkpoint.json').exists())
        self.assertEqual({call[1] for call in client.calls if call[0] == 'match_ranking_by_share'}, {'100'})

    def test_incomplete_million_group_cannot_publish_even_with_complete_thousand(self):
        self.thousand()
        client = FakeClient(failures={('match_ranking_by_share', '100', 1):
                                     collector.SourceError('million roster unavailable')})
        with mock.patch.object(collector, 'PublicClient', return_value=client), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(collector.SourceError, '百万组 collection incomplete'):
                collector.run(Namespace(snapshot=self.path, interval=2))
        self.assertFalse((self.path / 'group-holdings.json').exists())
        self.assertTrue((self.path / 'group-100-checkpoint.json').exists())
        self.assertEqual(client.calls, [('match_ranking_by_share', '100', 1)])

    def test_source_holder_count_mismatch_retains_every_checkpoint_and_raw_file(self):
        path = self.path / 'all-hot-stocks.json'
        base = json.loads(path.read_text())
        base['records'][0]['holder_count'] = 1
        collector.save(path, base)
        self.snapshot = collector.read_snapshot(self.path)
        self.thousand()
        thousand_checkpoint = self.path / 'thousand-group-checkpoint.json'
        thousand_checkpoint.write_text('{"retained": true}', encoding='utf-8')
        thousand_raw = self.path / 'competitions' / 'test' / 'raw' / 'thousand-group' / 'held.json'
        collector.save(thousand_raw, {'retained': True})
        client = FakeClient(pages={(code, 1): [member('member-' + code, 1, code)] for code in ('100',)})
        with mock.patch.object(collector, 'PublicClient', return_value=client), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(collector.SourceError, 'checkpoints retained'):
                collector.run(Namespace(snapshot=self.path, interval=2))
        self.assertEqual(thousand_checkpoint.read_text(), '{"retained": true}')
        self.assertTrue(thousand_raw.exists())
        for code in ('100',):
            self.assertTrue((self.path / f'group-{code}-checkpoint.json').exists())
            positions = self.path / 'competitions' / 'test' / 'raw' / f'group-{code}' / 'positions'
            self.assertTrue(any(positions.glob('*.json')))
        merged = json.loads((self.path / 'group-holdings.json').read_text())
        validation = collector.load_capital_groups(self.snapshot, collector.validate_weights({
            'version': merged['metadata']['weight_version'], 'weights': merged['metadata']['weights']}),
            self.path / 'group-holdings.json')
        self.assertFalse(validation['available'])
        self.assertIn('高于官方热榜人数', '; '.join(validation['warnings']))


if __name__ == '__main__':
    unittest.main()
