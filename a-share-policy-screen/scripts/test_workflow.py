"""Synthetic checks of data gates, provenance, B2 and portable report output."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from screen import evaluate
from reporting import view_model, write_reports
from fetch_data import reusable_bundle
from test_policies import bars


def fixture(n=120):
    rows=bars(n)
    return {'schema_version':'1.0','target_date':rows[-1]['date'],'scope':'subset',
            'universe_source':'SYNTHETIC_TEST_ONLY','calendar_source':'SYNTHETIC_TEST_ONLY',
            'market_days':[r['date'] for r in rows], 'stocks':[{
                'symbol':'sh600001','name':'Synthetic <img src=x onerror=alert(1)>','asset_status':'ACTIVE',
                'adjust':'qfq','bar_source':'synthetic','bars_as_of':rows[-1]['date']+'T16:00:00+08:00',
                'volume_unit':'lots','volume_unit_source':'synthetic','bars':rows,
                'finance':{'total_shares':800000000,'source':'synthetic'},
                'boards':{'listing_board':'MAIN','source':'synthetic'}, 'quote':{}}]}


class WorkflowTests(unittest.TestCase):
    def test_all_policies_keep_missing_vs_zero(self):
        result=evaluate(fixture())
        p={p['id']:p for p in result['policies']}
        self.assertEqual(p['B1']['status'],'completed')
        self.assertEqual(p['B1']['hit_count'],0)
        self.assertEqual(p['MA_GOLDEN_CROSS_FIRST_PULLBACK']['missing'],1)
        self.assertEqual(p['B2_V1']['status'],'source_unavailable')
        self.assertIsNone(p['B2_V1']['hit_count'])

    def test_future_rows_excluded_before_indicators(self):
        bundle=fixture();target=bundle['target_date'];s=bundle['stocks'][0]
        s['bars'].append({**s['bars'][-1],'date':(date.fromisoformat(target)+timedelta(days=1)).isoformat(),'close':0})
        self.assertEqual(evaluate(bundle,['LOW_VOLUME_STABLE_PRICE'])['policies'][0]['hit_count'],1)

    def test_unexplained_gap_not_filled(self):
        bundle=fixture();del bundle['stocks'][0]['bars'][30]
        p=evaluate(bundle,['B1'])['policies'][0]
        self.assertEqual(p['status'],'not_evaluated')
        self.assertEqual(p['missing'],1)

    def test_before_close_not_evaluated(self):
        b=fixture();b['stocks'][0]['bars_as_of']=b['target_date']+'T14:59:59+08:00'
        self.assertEqual(evaluate(b,['B1'])['policies'][0]['evaluated'],0)

    def test_volume_shares_normalized_without_changing_ratios(self):
        b=fixture();baseline=evaluate(b,['LOW_VOLUME_STABLE_PRICE'])
        b['stocks'][0]['volume_unit']='shares'
        for r in b['stocks'][0]['bars']:r['volume']*=100
        normalized=evaluate(b,['LOW_VOLUME_STABLE_PRICE'])
        self.assertEqual(baseline['policies'],normalized['policies'])
        self.assertEqual(view_model(normalized)['stocks']['sh600001']['daily']['volume_wan'],.01)

    def test_unknown_volume_unit_stays_missing(self):
        b=fixture();b['stocks'][0]['volume_unit']='unknown'
        self.assertEqual(evaluate(b,['B1'])['policies'][0]['evaluated'],0)

    def test_finance_provenance_required_for_gate(self):
        b=fixture();b['stocks'][0]['finance'].pop('source')
        self.assertEqual(evaluate(b,['LOW_VOLUME_STABLE_PRICE'])['policies'][0]['evaluated'],0)

    def test_unavailable_universe_fails(self):
        b=fixture();b['stocks']=[]
        with self.assertRaisesRegex(ValueError,'universe'):evaluate(b)

    def test_duplicate_universe_fails(self):
        b=fixture();b['stocks'].append(copy.deepcopy(b['stocks'][0]))
        with self.assertRaisesRegex(ValueError,'duplicate'):evaluate(b)

    def test_report_portable_dates_short_history_and_safe_names(self):
        b=fixture(21)
        b['stocks'][0]['finance']['total_shares']=1000000000
        for i in [4,10]:b['stocks'][0]['bars'][i].update(close=10.1,high=10.1,volume=200)
        b['stocks'][0]['bars'][-2].update(close=9.9,low=9.9)
        b['stocks'][0]['bars'][-1].update(close=9.8,low=9.8)
        r=evaluate(b,['B1']);m=view_model(r)
        self.assertEqual(len(m['stocks']['sh600001']['daily']['chart']),21)
        self.assertIsNone(m['stocks']['sh600001']['daily']['chart'][0]['ma5'])
        with tempfile.TemporaryDirectory() as d:
            write_reports(r,d)
            content=Path(d,'report.inline.html').read_text()
            self.assertNotIn('2026-09-29 收盘',content)
            self.assertNotIn('<img src=x',content)
            self.assertIn('&lt;img',content)
            self.assertIn(b['target_date'],content)
            self.assertNotIn('__DATA_JSON__',content)

    def test_valid_empty_b2_snapshot_is_completed_zero(self):
        b=fixture();p=b['market_days'][-2]
        snap={'policy_id':'B1_V2','version':'b1_yellow_white_v2.0','trade_date':p,'saved_at':p+'T16:00:00+08:00',
              'status':'success','candidates':[]}
        result=evaluate(b,['B2_V1'],snapshot=snap)['policies'][0]
        self.assertEqual((result['status'],result['pool'],result['hit_count']),('completed',0,0))

    def test_partial_snapshot_stock_remains_in_denominator(self):
        b=fixture();p=b['market_days'][-2]
        snap={'policy_id':'B1_V2','version':'b1_yellow_white_v2.0','trade_date':p,'saved_at':p+'T16:00:00+08:00',
              'status':'success','candidates':[{'symbol':'sh600002','signal_date':p}]}
        r=evaluate(b,['B2_V1'],snapshot=snap)['policies'][0]
        self.assertEqual((r['pool'],r['missing'],r['status']),(1,1,'not_evaluated'))

    def test_bad_member_does_not_abort_valid_member(self):
        for bad_field in ('date', 'nan_close'):
            with self.subTest(bad_field=bad_field):
                b=fixture(); bad=copy.deepcopy(b['stocks'][0]);bad['symbol']='sh600002'
                if bad_field=='date':bad['bars'][-1].pop('date')
                else:bad['bars'][-1]['close']=float('nan')
                b['stocks'].append(bad)
                result=evaluate(b,['LOW_VOLUME_STABLE_PRICE']);p=result['policies'][0]
                self.assertEqual((p['status'],p['pool'],p['evaluated'],p['missing'],p['hit_count']),('partial',2,1,1,1))
                self.assertEqual(p['hits'],['sh600001'])
                with tempfile.TemporaryDirectory() as d:
                    write_reports(result,d)
                    json.dumps(result,allow_nan=False)

    def test_invalid_snapshot_time_is_unavailable(self):
        b=fixture();p=b['market_days'][-2]
        for stamp in ('NOT_A_DATE','2099-01-01T16:00:00+08:00',p+'T14:59:59+08:00',p+'T16:00:00',b['target_date']+'T15:00:01+08:00'):
            with self.subTest(stamp=stamp):
                snap={'policy_id':'B1_V2','version':'b1_yellow_white_v2.0','trade_date':p,
                      'saved_at':stamp,'status':'success','candidates':[]}
                result=evaluate(b,['B2_V1'],snapshot=snap)['policies'][0]
                self.assertEqual(result['status'],'source_unavailable')
                self.assertIsNone(result['hit_count'])

    def test_missing_or_corrupt_snapshot_keeps_other_policies(self):
        for corrupt in (False,True):
            with self.subTest(corrupt=corrupt),tempfile.TemporaryDirectory() as d:
                root=Path(d);source=root/'input.json';source.write_text(json.dumps(fixture()))
                snap=root/'snapshot.json'
                if corrupt:snap.write_text('{bad json')
                process=subprocess.run([sys.executable,str(Path(__file__).with_name('screen.py')),
                    '--input',str(source),'--out',str(root/'run'),'--b2-snapshot',str(snap)],capture_output=True,text=True)
                self.assertEqual(process.returncode,0,process.stderr)
                result=json.loads((root/'run/results.json').read_text());p={p['id']:p for p in result['policies']}
                self.assertEqual(len(p),7)
                self.assertEqual(p['B2_V1']['status'],'source_unavailable')
                self.assertEqual(p['LOW_VOLUME_STABLE_PRICE']['hit_count'],1)
                self.assertTrue((root/'run/report.html').is_file())

    def test_large_report_preserves_complete_file(self):
        b=fixture();b['universe_source']='x'*1_000_000
        result=evaluate(b,['LOW_VOLUME_STABLE_PRICE'])
        with tempfile.TemporaryDirectory() as d:
            write_reports(result,d);root=Path(d)
            self.assertFalse((root/'report.inline.html').exists())
            self.assertTrue((root/'report.html').exists())
            manifest=json.loads((root/'render-manifest.json').read_text())
            self.assertFalse(manifest['inline_available'])
            self.assertEqual(manifest['candidate_count'],1)

    def test_reuse_input_is_same_day_qfq_and_network_free(self):
        b=fixture();b['collection_request']={'start_date':b['market_days'][0],'end_date':b['target_date'],'lookback_days':100,'adjust':'qfq'}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source.json';out=root/'copied.json'
            source.write_text(json.dumps(b))
            loaded,digest=reusable_bundle(source,b['target_date'],100)
            self.assertEqual(loaded,b);self.assertEqual(len(digest),64)
            process=subprocess.run([sys.executable,str(Path(__file__).with_name('fetch_data.py')),
                '--date',b['target_date'],'--lookback-days','100','--reuse-input',str(source),'--out',str(out)],capture_output=True,text=True)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertEqual(json.loads(out.read_text()),b)
            self.assertEqual(json.loads(process.stdout)['network_requests'],0)

    def test_reuse_input_rejects_stale_or_non_qfq(self):
        b=fixture();b['collection_request']={'start_date':b['market_days'][0],'end_date':b['target_date'],'lookback_days':100,'adjust':'qfq'}
        with tempfile.TemporaryDirectory() as d:
            source=Path(d,'source.json')
            for mutation in ('stale','unadjusted'):
                bad=copy.deepcopy(b)
                if mutation=='stale':bad['stocks'][0]['bars'][-1]['date']=bad['market_days'][-2]
                else:bad['stocks'][0]['adjust']='none'
                source.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):reusable_bundle(source,b['target_date'],100)

    def test_reuse_input_rejects_unknown_or_short_collection_window(self):
        b=fixture()
        with tempfile.TemporaryDirectory() as d:
            source=Path(d,'source.json')
            for request in (None,{'start_date':b['market_days'][-10],'end_date':b['target_date'],'lookback_days':10,'adjust':'qfq'}):
                candidate=copy.deepcopy(b)
                if request:candidate['collection_request']=request
                source.write_text(json.dumps(candidate))
                with self.assertRaisesRegex(ValueError,'history_window'):
                    reusable_bundle(source,b['target_date'],100)


if __name__=='__main__':unittest.main(verbosity=2)
