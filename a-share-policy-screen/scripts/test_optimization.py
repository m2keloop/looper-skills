"""Behavioral regressions for immutable caching, limited requests and offline recovery."""
import copy
import json
import gzip
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from policies import DEFAULT_POLICIES, POLICIES, run_policy
from request_control import HTTPClient
from run_daily import execute, natural_history_complete, resolve_latest, run_lock, update_bars
from screen import evaluate, rebuild_report, run
from storage import Store, load_input
from test_workflow import fixture


class Frame:
    def __init__(self, rows):
        self.rows=rows
    def to_dict(self, _):
        return self.rows


class Market:
    def __init__(self, responses):
        self.responses=iter(responses)
        self.calls=[]
    def tencent_kline(self,sid,**kwargs):
        self.calls.append(kwargs)
        return Frame(next(self.responses))


class OptimizationTests(unittest.TestCase):
    def test_formula_change_reuses_snapshot_and_preserves_old_result(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);bundle=fixture();bundle['scope']='current_active_sh_sz'
            store=Store(root/'_cache/market.sqlite');store.import_bundle(bundle);store.close()
            args=SimpleNamespace(root=root,date=bundle['target_date'],offline=True,import_input=None,
                run_dir=None,policies='all',yellow_filter=False,require_pullback=False,
                main_board_only=False,retry_failures=False,request_interval=.25)
            first=execute(args);old_dir=root/bundle['target_date']
            old_path=old_dir/'result/results.json';legacy=json.loads(old_path.read_text())
            legacy.pop('policy_versions')
            for policy in legacy['policies']:policy.pop('version')
            old_path.write_text(json.dumps(legacy));original=old_path.read_bytes()
            upgraded=execute(args)
            self.assertEqual(old_path.read_bytes(),original)
            self.assertNotEqual(first['out'],upgraded['out'])
            self.assertEqual(upgraded['run_metrics']['network_requests'],0)
            self.assertEqual(json.loads(Path(upgraded['manifest']).read_text())['snapshot_id'],
                             json.loads((old_dir/'input-manifest.json').read_text())['snapshot_id'])
            again=execute(args)
            self.assertTrue(again['reused'])
            self.assertEqual(again['out'],upgraded['out'])
            args.run_dir=old_dir
            with self.assertRaisesRegex(FileExistsError,'formula_differ'):execute(args)
            self.assertEqual(old_path.read_bytes(),original)

    def test_six_policies_and_removed_id_rejected(self):
        self.assertEqual(len(POLICIES),6)
        self.assertNotIn('B2_V1',POLICIES)
        with self.assertRaises(ValueError):
            evaluate(fixture(),['B2_V1'])

    def test_prevalidated_path_preserves_all_six_formulas(self):
        bundle=fixture(741);s=bundle['stocks'][0]
        for pid in POLICIES:
            kwargs={'finance':s['finance'],'boards':s['boards']}
            self.assertEqual(run_policy(pid,s['bars'],bundle['target_date'],**kwargs),
                             run_policy(pid,s['bars'],bundle['target_date'],_validated=True,**kwargs))

    def test_short_complete_listing_only_fetches_recent(self):
        b=fixture(30);s=b['stocks'][0];s['list_date']=s['bars'][0]['date']
        target=b['market_days'][-1];s['bars']=s['bars'][:-1]
        market=Market([b['stocks'][0]['bars'][-2:]+[{'date':target,'open':10,'high':10,'low':10,'close':10,'volume':100}]])
        rows,decision=update_bars(market,s,target,b['market_days'][0],b['market_days'])
        self.assertEqual(len(market.calls),1)
        self.assertFalse(decision['full_refresh'])
        self.assertEqual(len(rows),30)

    def test_real_missing_history_is_refreshed_once(self):
        b=fixture(120);s=b['stocks'][0];full=copy.deepcopy(s['bars']);s['bars']=full[-21:-1]
        market=Market([full[-3:],full])
        rows,decision=update_bars(market,s,b['target_date'],b['market_days'][0],b['market_days'])
        self.assertEqual(len(market.calls),2)
        self.assertEqual(decision['reason'],'missing_history')
        s.update(bars=rows,history_coverage=decision['history_coverage'])
        self.assertTrue(natural_history_complete(s,b['market_days'][0],b['market_days']))

    def test_qfq_mismatch_requires_full_fresh_history(self):
        b=fixture(30);s=b['stocks'][0];full=copy.deepcopy(s['bars']);s['bars']=copy.deepcopy(full[:-1])
        recent=copy.deepcopy(full[-3:]);recent[-2]['close']=9.9;recent[-2]['low']=9.9
        full[-2]=recent[-2]
        market=Market([recent,full])
        rows,decision=update_bars(market,s,b['target_date'],b['market_days'][0],b['market_days'])
        self.assertEqual(decision['reason'],'qfq_mismatch')
        self.assertEqual(rows,full)
        market=Market([recent,full[:-1]])
        with self.assertRaisesRegex(ValueError,'no_target_day'):
            update_bars(market,s,b['target_date'],b['market_days'][0],b['market_days'])

    def test_stale_recent_is_not_a_success(self):
        b=fixture(30);s=b['stocks'][0];s['bars']=s['bars'][:-1]
        market=Market([s['bars'][-3:]])
        with self.assertRaisesRegex(ValueError,'unavailable'):
            update_bars(market,s,b['target_date'],b['market_days'][0],b['market_days'])
        self.assertEqual(len(market.calls),1)

    def test_stale_long_endpoint_extends_only_with_matching_recent_qfq(self):
        b=fixture(120);s=b['stocks'][0];full=copy.deepcopy(s['bars']);s['bars']=full[-21:-1]
        market=Market([full[-3:],full[:-1]])
        rows,decision=update_bars(market,s,b['target_date'],b['market_days'][0],b['market_days'])
        self.assertEqual(rows,full)
        self.assertTrue(decision['full_refresh'])

    def test_holiday_resolves_previous_close_and_caches_month(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);header={'target_date':'2026-09-30','market_days':['2026-09-29','2026-09-30'],
                                'calendar_source':'official_test_fixture'}
            calls=[]
            def fetch(start,end):
                calls.append((start,end));return ['2026-10-08','2026-10-09','2026-10-30']
            day,_=resolve_latest(root,header,'2026-10-01',fetch_calendar=fetch)
            self.assertEqual(day,'2026-09-30')
            day,_=resolve_latest(root,header,'2026-10-02',fetch_calendar=fetch)
            self.assertEqual(day,'2026-09-30')
            self.assertEqual(len(calls),1)

    def test_store_append_and_correction_preserve_old_snapshots(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'market.sqlite');b=fixture(30);s=b['stocks'][0]
            old={**s,'bars':s['bars'][:-1]};revision=store.put_series(old)
            appended=store.put_series(s,revision,old['bars'])
            self.assertEqual(store.bars(revision),old['bars'])
            self.assertEqual(store.bars(appended),s['bars'])
            corrected=copy.deepcopy(s);corrected['bars'][0]['close']=9
            corrected_revision=store.put_series(corrected,appended,s['bars'])
            self.assertEqual(store.bars(appended),s['bars'])
            self.assertEqual(store.bars(corrected_revision),corrected['bars'])
            self.assertIsNone(store.db.execute('SELECT parent FROM series WHERE id=?',(corrected_revision,)).fetchone()[0])
            store.close()

    def test_manifest_roundtrip_and_immutable_publication(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'market.sqlite');b=fixture();snapshot=store.import_bundle(b)
            path=Path(d)/'day/input-manifest.json';store.manifest(snapshot,path)
            loaded,_=load_input(path)
            self.assertEqual(list(loaded['stocks']),b['stocks'])
            changed=copy.deepcopy(b);changed['stocks'][0]['name']='changed'
            other=store.import_bundle(changed)
            with self.assertRaises(FileExistsError):store.manifest(other,path)
            store.close()

    def test_report_failure_preserves_calculation_and_rebuilds_offline(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'input.json';source.write_text(json.dumps(fixture()))
            out=Path(d)/'result'
            with patch('reporting.write_reports',side_effect=RuntimeError('report-only-failure')):
                with self.assertRaises(RuntimeError):run(source,out)
            result=json.loads((out/'results.json').read_text())
            self.assertTrue((out/'calculation-status.json').exists())
            self.assertNotIn('bars',result['stocks']['sh600001'])
            self.assertEqual([p['id'] for p in result['policies']],DEFAULT_POLICIES)
            with patch('socket.socket.connect',side_effect=AssertionError('network forbidden')):
                rebuild_report(out)
            self.assertTrue((out/'report.html').exists())

    def test_both_cli_entrypoints_default_and_all_render_only_current_three(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);bundle=fixture();source=root/'input.json'
            source.write_text(json.dumps(bundle))
            for name in ('screen.py','run_daily.py'):
                for explicit_all in (False,True):
                    with self.subTest(entrypoint=name,explicit_all=explicit_all):
                        out=root/(name.replace('.py','')+str(explicit_all))
                        command=[sys.executable,str(Path(__file__).with_name(name))]
                        if name=='screen.py':
                            command+=['--input',str(source),'--out',str(out)]
                        else:
                            command+=['--root',str(out),'--date',bundle['target_date'],
                                      '--import-input',str(source),'--offline']
                        if explicit_all:command+=['--policies','all']
                        process=subprocess.run(command,capture_output=True,text=True)
                        self.assertEqual(process.returncode,0,process.stderr)
                        report_dir=out if name=='screen.py' else out/bundle['target_date']/'result'
                        result=json.loads((report_dir/'results.json').read_text())
                        model=json.loads(gzip.decompress((report_dir/'view-model.json.gz').read_bytes()))
                        self.assertEqual([p['id'] for p in result['policies']],DEFAULT_POLICIES)
                        self.assertEqual([p['id'] for p in model['policies']],DEFAULT_POLICIES)

    def test_global_limiter_reuses_session_and_waits_between_request_starts(self):
        class Session:
            def __init__(self):self.calls=[]
            def request(self,method,url,**kwargs):
                self.calls.append(url)
                return type('Response',(),{'status_code':200})()
        now=[0.0];session=Session()
        def sleep(seconds):now[0]+=seconds
        client=HTTPClient(.25,session,lambda:now[0],sleep)
        for _ in range(3):client.request('GET','https://example.invalid/q?public=1')
        self.assertEqual(client.metrics()['http_requests'],3)
        self.assertEqual(client.metrics()['request_wait_seconds'],.5)
        self.assertEqual(len(session.calls),3)
        self.assertEqual(client.metrics()['endpoints'],{'example.invalid/q':3})

    def test_duplicate_collectors_are_locked(self):
        with tempfile.TemporaryDirectory() as d:
            with run_lock(d):
                with self.assertRaisesRegex(RuntimeError,'already_running'):
                    with run_lock(d):pass


if __name__=='__main__':unittest.main()
