"""Exercise the cached-candle CLI without allowing market-source requests."""
import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('contest_candle_review', ROOT / 'scripts' / 'review_hot_stock_klines.py')
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


class OfflineCandleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sample = json.loads((ROOT / 'assets' / 'sample-candles.json').read_text(encoding='utf-8'))
        self.cache = self.root / 'candles.json'
        self.cache.write_text(json.dumps(self.sample), encoding='utf-8')
        self.analysis = self.root / 'analysis.json'
        self.write_analysis(self.sample['stocks'])
        self.output = self.root / 'output'

    def write_analysis(self, stocks):
        ranking = [{key: stock[key] for key in ('stock_code', 'stock_name', 'holder_count_sum', 'contest_count')}
                   for stock in stocks]
        self.analysis.write_text(json.dumps({'metadata': {'run_id': 'offline-test'}, 'ranking': ranking}), encoding='utf-8')

    def invoke(self, *arguments, offline=True):
        argv = ['review_hot_stock_klines.py', '--analysis', str(self.analysis), '--output', str(self.output)]
        if offline:
            argv += ['--candles', str(self.cache)]
        argv += list(arguments)
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
            return review.main()

    def test_sample_produces_all_artifacts_without_network_and_preserves_source_time(self):
        # Cached derived metrics must never substitute for recalculation.
        payload = copy.deepcopy(self.sample)
        payload['stocks'][0]['metrics']['return_20d_pct'] = 123456
        self.cache.write_text(json.dumps(payload), encoding='utf-8')
        with patch.object(review, 'request', side_effect=AssertionError('Network request forbidden')) as request, \
                patch.object(review.subprocess, 'run', side_effect=AssertionError('curl forbidden')) as curl:
            self.invoke('--cutoff', '2026-10-09')
        request.assert_not_called()
        curl.assert_not_called()
        self.assertEqual({path.name for path in self.output.iterdir()}, {'review.json', 'candles.json', 'index.html'})
        candles = json.loads((self.output / 'candles.json').read_text(encoding='utf-8'))
        summary = json.loads((self.output / 'review.json').read_text(encoding='utf-8'))
        self.assertEqual(candles['metadata']['fetched_at'], self.sample['metadata']['fetched_at'])
        self.assertEqual(candles['metadata']['latest_market_date'], '2026-10-09')
        self.assertEqual(candles['metadata']['input_mode'], 'offline-cache')
        self.assertEqual(len(candles['stocks']), 10)
        self.assertAlmostEqual(candles['stocks'][0]['metrics']['return_20d_pct'],
                               self.sample['stocks'][0]['metrics']['return_20d_pct'])
        self.assertNotIn('qfq', summary['stocks'][0])
        self.assertEqual(summary['stocks'][0]['metrics'], candles['stocks'][0]['metrics'])

    def test_omitted_cutoff_uses_cached_market_date(self):
        with patch.object(review, 'request', side_effect=AssertionError('Network request forbidden')):
            self.invoke()
        candles = json.loads((self.output / 'candles.json').read_text(encoding='utf-8'))
        self.assertEqual(candles['metadata']['cutoff_requested'], '2026-10-09')
        self.assertEqual(candles['metadata']['latest_market_date'], '2026-10-09')

    def test_explicit_earlier_cutoff_filters_real_bars(self):
        with patch.object(review, 'request', side_effect=AssertionError('Network request forbidden')):
            self.invoke('--cutoff', '2026-09-30')
        candles = json.loads((self.output / 'candles.json').read_text(encoding='utf-8'))
        self.assertEqual(candles['metadata']['latest_market_date'], '2026-09-30')
        self.assertEqual(candles['metadata']['fetched_at'], self.sample['metadata']['fetched_at'])
        self.assertTrue(all(stock['qfq'][-1]['date'] == '2026-09-30' for stock in candles['stocks']))

    def test_missing_selected_stock_fails_without_output_or_requests(self):
        payload = copy.deepcopy(self.sample)
        payload['stocks'].pop()
        self.cache.write_text(json.dumps(payload), encoding='utf-8')
        with patch.object(review, 'request', side_effect=AssertionError('Network request forbidden')):
            with self.assertRaisesRegex(ValueError, 'missing selected stocks'):
                self.invoke()
        self.assertFalse(self.output.exists())

    def test_invalid_source_dates_and_prices_fail_before_output(self):
        mutations = (
            lambda payload: payload['metadata'].update(source='unknown source'),
            lambda payload: payload['metadata'].update(fetched_at='2026-10-10T14:54:02'),
            lambda payload: payload['stocks'][0]['sources'].update(qfq='https://example.com/unknown'),
            lambda payload: payload['stocks'][0]['qfq'][0].update(date='not-a-date'),
            lambda payload: payload['stocks'][0]['qfq'][1].update(date=payload['stocks'][0]['qfq'][0]['date']),
            lambda payload: payload['stocks'][0]['qfq'][0].update(low=999999),
            lambda payload: payload['stocks'][0]['qfq'][0].update(volume=float('nan')),
            lambda payload: payload['stocks'][0].update(unadjusted=[]),
            lambda payload: payload['stocks'][0]['unadjusted'].pop(1),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                payload = copy.deepcopy(self.sample)
                mutate(payload)
                self.cache.write_text(json.dumps(payload), encoding='utf-8')
                with patch.object(review, 'request', side_effect=AssertionError('Network request forbidden')):
                    with self.assertRaises(ValueError):
                        self.invoke()
                self.assertFalse(self.output.exists())

    def test_insufficient_history_after_cutoff_fails_without_output(self):
        with patch.object(review, 'request', side_effect=AssertionError('Network request forbidden')):
            with self.assertRaisesRegex(ValueError, 'Not enough comparable daily history'):
                self.invoke('--cutoff', '2026-04-13')
        self.assertFalse(self.output.exists())

    def test_zero_volume_baseline_fails_instead_of_inventing_ratio(self):
        payload = copy.deepcopy(self.sample)
        for row in payload['stocks'][0]['qfq'][-21:-1]:
            row['volume'] = 0
        self.cache.write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'nonzero daily volume'):
            self.invoke()
        self.assertFalse(self.output.exists())

    def test_without_candles_keeps_live_request_path(self):
        stock = self.sample['stocks'][0]
        self.write_analysis([stock])
        with patch.object(review, 'request', side_effect=[
                (stock['qfq'], stock['sources']['qfq'], 1),
                (stock['unadjusted'], stock['sources']['unadjusted'], 2)]) as request:
            self.invoke('--cutoff', '2026-10-09', offline=False)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args, ('sh600127', 'qfq', 0))
        self.assertEqual(request.call_args_list[1].args, ('sh600127', '', 1))
        candles = json.loads((self.output / 'candles.json').read_text(encoding='utf-8'))
        self.assertEqual(candles['metadata']['input_mode'], 'live')
        self.assertEqual(candles['metadata']['latest_market_date'], '2026-10-09')


if __name__ == '__main__':
    unittest.main()
