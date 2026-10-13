"""Synthetic checks of data gates, provenance and portable report output."""
import copy
import gzip
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
from test_b1_structure import shape_rows
from policies import DEFAULT_POLICIES, POLICIES


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
        result=evaluate(fixture(), POLICIES)
        p={p['id']:p for p in result['policies']}
        self.assertEqual(p['B1']['status'],'completed')
        self.assertEqual(p['B1']['hit_count'],0)
        self.assertEqual(p['MA_GOLDEN_CROSS_FIRST_PULLBACK']['missing'],1)
        self.assertEqual(len(p),6)
        self.assertNotIn('B2_V1',p)

    def test_default_selects_only_current_three_with_same_decisions(self):
        bundle=fixture()
        current=evaluate(bundle)
        explicit=evaluate(bundle,DEFAULT_POLICIES)
        self.assertEqual([p['id'] for p in current['policies']],
                         ['B1','B1_WEEKLY','MA_GOLDEN_CROSS_FIRST_PULLBACK'])
        self.assertEqual(current['policies'],explicit['policies'])

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
        b['stocks'][0]['bars']=shape_rows()
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

    def test_b1_rank_order_and_report_score_match_saved_evidence(self):
        b=fixture(21);s=b['stocks'][0];s['bars']=shape_rows()
        s['finance']['total_shares']=1_000_000_000
        other=copy.deepcopy(s);other['symbol']='sh600002'
        other['bars']=shape_rows(fractions=(.25,.2,.15));b['stocks'].append(other)
        report=evaluate(b,['B1']);model=view_model(report);p=report['policies'][0]
        self.assertEqual(p['hits'],['sh600002','sh600001'])
        self.assertTrue(model['policies'][0]['ranked'])
        for row in p['results']:
            self.assertEqual(row['metrics']['shape_score'],model['policies'][0]['shapes'][row['symbol']]['score'])
        with tempfile.TemporaryDirectory() as d:
            write_reports(report,d)
            text=Path(d,'report.md').read_text()
            self.assertIn('形态分 | 异动日',text)
            self.assertLess(text.index('600002.SH'),text.index('600001.SH'))

    def test_old_b1_report_and_other_policies_do_not_inherit_shape_scores(self):
        b=fixture(160);b['stocks'][0]['bars']=shape_rows(n=160)
        b['stocks'][0]['finance']['total_shares']=1_000_000_000
        report=evaluate(b,['B1','B1_WEEKLY'])
        model=view_model(report)
        self.assertFalse(model['policies'][1]['ranked'])
        self.assertEqual(model['policies'][1]['shapes'],{})
        saved_score=model['policies'][0]['shapes']['sh600001']['score']
        report['policies'][0]['version']='b1_volume_contraction_v2.0'
        old_scored=view_model(report)
        self.assertTrue(old_scored['policies'][0]['ranked'])
        self.assertEqual(old_scored['policies'][0]['shapes']['sh600001']['score'],saved_score)
        report['policies'][0].pop('version')
        legacy=view_model(report)
        self.assertFalse(legacy['policies'][0]['ranked'])
        self.assertEqual(legacy['policies'][0]['shapes'],{})



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



    def test_large_report_preserves_complete_file(self):
        b=fixture();b['universe_source']='x'*1_000_000
        result=evaluate(b,['LOW_VOLUME_STABLE_PRICE'])
        with tempfile.TemporaryDirectory() as d:
            write_reports(result,d);root=Path(d)
            self.assertTrue((root/'report.html').exists())
            restored=json.loads(gzip.decompress((root/'view-model.json.gz').read_bytes()))
            self.assertIn('x'*1_000_000,restored['notes'][0])
            manifest=json.loads((root/'render-manifest.json').read_text())
            self.assertEqual(manifest['inline_available'],(root/'report.inline.html').exists())
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
