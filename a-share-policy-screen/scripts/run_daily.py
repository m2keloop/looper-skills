"""Unified resumable daily screening: one cache, three default policies, offline reports."""
import argparse
import fcntl
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fetch_data import batch_quotes, bounded, calendar_range, is_main_board
from policies import DEFAULT_POLICIES, POLICIES, POLICY_VERSIONS, saved_policy_versions
from screen import rebuild_report, run, symbol
from storage import Store, atomic_json, load_input

CN = ZoneInfo('Asia/Shanghai')


@contextmanager
def run_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root/'.run.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('daily_collection_already_running')
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def same_bar(left, right):
    return all(abs(float(left[k])-float(right[k])) <= 1e-7*max(1.0,abs(float(left[k])))
               for k in ('open','high','low','close','volume'))


def frame_rows(frame, target):
    return [{k:r[k] for k in ('date','open','high','low','close','volume')}
            for r in frame.to_dict('records') if r['date'] <= target]


def natural_history_complete(stock, start, calendar):
    coverage = stock.get('history_coverage') or {}
    if coverage.get('requested_start') == start and coverage.get('request_completed'):
        return True
    rows, listing = stock.get('bars') or [], stock.get('list_date')
    if len(rows) >= 741:
        return True
    if listing and listing >= start and rows:
        first_market = min((d for d in calendar if d >= listing), default=None)
        return rows[0]['date'] == first_market
    return False


def update_bars(sd, stock, target, start, calendar):
    old = stock.get('bars') or []
    gap = sum(d > old[-1]['date'] and d <= target for d in calendar) if old else 1
    if gap > 637:
        recent = frame_rows(sd.tencent_kline(stock['symbol'], start=old[-1]['date'], end=target, adjust='qfq'),target)
    else:
        recent = frame_rows(sd.tencent_kline(stock['symbol'], count=max(3,gap+2), adjust='qfq'),target)
    if not recent or recent[-1]['date'] != target:
        raise ValueError('target_day_qfq_bar_unavailable')
    by_day = {r['date']:r for r in old}
    overlap = [r for r in recent if r['date'] in by_day]
    rebased = not overlap or any(not same_bar(by_day[r['date']],r) for r in overlap)
    reason = 'new_stock_or_no_overlap' if not overlap else 'qfq_mismatch' if rebased else 'missing_history' if not natural_history_complete(stock,start,calendar) else None
    if reason:
        full = frame_rows(sd.tencent_kline(stock['symbol'],start=start,end=target,adjust='qfq'),target)
        if full and full[-1]['date'] == target:
            return full, {'full_refresh':True,'reason':reason,'overlap_verified':len(overlap),
                          'history_coverage':{'requested_start':start,'requested_end':target,'request_completed':True}}
        if rebased:
            raise ValueError('refreshed_history_has_no_target_day')
        # The old qfq overlap was verified. A stale long endpoint must not discard a valid new bar.
        full_by_day = {r['date']:r for r in full}
        full_overlap = [r for r in recent if r['date'] in full_by_day]
        if full_overlap and all(same_bar(full_by_day[r['date']],r) for r in full_overlap):
            # Extend from the fetched historical series only after verifying it against recent qfq.
            full_by_day.update((r['date'],r) for r in recent)
            return [full_by_day[d] for d in sorted(full_by_day)], {
                'full_refresh':True,'reason':reason,'overlap_verified':len(full_overlap),
                'history_coverage':{'requested_start':start,'requested_end':target,'request_completed':True}}
        coverage = stock.get('history_coverage') or {}
    else:
        coverage = stock.get('history_coverage') or {'requested_start':start,'requested_end':target,'request_completed':True}
    by_day.update((r['date'],r) for r in recent)
    return [by_day[d] for d in sorted(by_day)], {'full_refresh':False,'reason':reason,
                'overlap_verified':len(overlap),'history_coverage':coverage}


def apply_quote(stock, fields, target):
    if len(fields)<53 or len(fields[30])!=14 or fields[30][:8]!=target.replace('-','') or fields[30][8:]<'150000':
        raise ValueError('target_day_after_close_quote_unavailable')
    latest = stock['bars'][-1]
    amount = float(fields[37] or 0)
    if amount<=0 or latest['volume']<=0:
        raise ValueError('target_day_volume_or_amount_unavailable')
    if stock.get('volume_unit') not in ('lots','shares') or not stock.get('volume_unit_source'):
        implied = amount*10000/latest['volume']
        low, high = float(fields[34] or 0), float(fields[33] or 0)
        choices = [div for div in (1,100) if low<=implied*div/100<=high]
        if len(choices)!=1:
            raise ValueError('volume_unit_ambiguous')
        stock['volume_unit'] = 'shares' if choices[0]==100 else 'lots'
        stock['volume_unit_source'] = 'unique same-day amount/volume implied price within Tencent unadjusted H/L'
    stock['name'] = fields[1] or stock.get('name') or stock['symbol']
    stock['quote'] = {'date':target,'amount_yi':amount/10000,
                      'turnover_pct':float(fields[38]) if fields[38] else None}


def refresh_finance(sd, stock, fallback_failures=None):
    stock.pop('finance',None)
    try:
        info = sd.eastmoney_stock_info(stock['symbol'][2:])
        if info.get('industry'):
            stock['boards']['industries'] = [info['industry']]
        shares = float(info.get('total_shares') or 0)
        if not math.isfinite(shares) or shares<=0:
            raise ValueError('missing_total_shares')
        stock['finance'] = {'total_shares':shares,'source':'Eastmoney stock/get total_shares',
                            'as_of':datetime.now(timezone.utc).isoformat()}
    except Exception:
        if fallback_failures is not None and fallback_failures[0]>=3:
            stock['finance_error']='finance fallback circuit open after three failures'
            return
        try:
            stock['finance'] = bounded('finance',{'symbol':stock['symbol']},60)
        except Exception as exc:
            if fallback_failures is not None:
                fallback_failures[0]+=1
            stock['finance_error'] = type(exc).__name__+':finance_refresh_unavailable'


def collect(store, prior_id, target, run_dir, request_interval=0.25, retry_failures=False, main_board_only=False):
    import data_adapter as sd
    sd.V39_CLIENT.interval = request_interval
    header, old_entries = store.snapshot(prior_id)
    phase = time.perf_counter()
    calendar = list(header['market_days'])
    calendar_cache=store.path.parent/'calendar.json'
    if calendar_cache.exists():
        calendar=sorted(set(calendar+json.loads(calendar_cache.read_text())['days']))
    if target > max(calendar):
        calendar = sorted(set(calendar + calendar_range(sd,date.fromisoformat(max(calendar))+timedelta(days=1),date.fromisoformat(target))))
    if target not in calendar:
        raise ValueError('target_not_market_day')
    universe_path = run_dir/'universe.json'
    if universe_path.exists():
        universe = json.loads(universe_path.read_text())
    else:
        universe = bounded('universe',{'target':target},90)
        atomic_json(universe_path,universe)
    if not universe:
        raise ValueError('active_universe_unavailable')
    universe = sorted([{**s,'symbol':symbol(s['symbol']),'asset_status':'ACTIVE'} for s in universe],key=lambda s:s['symbol'])
    full_pool_size = len(universe)
    if main_board_only:
        universe = [s for s in universe if is_main_board(s['symbol'])]
        if not universe:
            raise ValueError('main_board_universe_unavailable')
    ids = [s['symbol'] for s in universe]
    if len(ids)!=len(set(ids)):
        raise ValueError('duplicate_universe_member')
    quotes = batch_quotes(sd,ids,run_dir/'quotes.json')
    preparation_seconds = time.perf_counter()-phase
    prior_by = {e['stock']['symbol']:e for e in old_entries}
    run_key = str(run_dir.resolve())+'|'+target
    completed = store.checkpoints(run_key)
    start = header.get('collection_request',{}).get('start_date') or (date.fromisoformat(target)-timedelta(days=1600)).isoformat()
    counts = {'pool':len(universe),'cache_hits':0,'success':0,'failed':0,'full_refreshes':0,'refresh_reasons':{}}
    entries=[];started=time.perf_counter();consecutive_network_failures=0;finance_failures=[0]
    for index,member in enumerate(universe,1):
        sid = member['symbol']
        saved = completed.get(sid)
        if saved and (not retry_failures or not saved['stock'].get('errors')):
            entries.append(saved);counts['cache_hits']+=1
            counts['failed' if saved['stock'].get('errors') else 'success']+=1
            continue
        prior = prior_by.get(sid)
        old = store.bars(prior['revision']) if prior else []
        stock = {**(prior['stock'] if prior else {}),**member,'bars':old,'errors':[],'adjust':'qfq'}
        stock.setdefault('bar_source','Tencent fqkline qfq via a-stock-data')
        stock.setdefault('boards',{'listing_board':'STAR' if sid.startswith(('sh688','sh689')) else 'CHINEXT' if sid.startswith(('sz300','sz301')) else 'MAIN',
                                  'source':'SH/SZ listing code classification'})
        try:
            bars, update = update_bars(sd,stock,target,start,calendar)
            stock['bars'] = bars
            apply_quote(stock,quotes.get(sid,[]),target)
            stock['bars_as_of'] = datetime.now(timezone.utc).isoformat()
            stock['history_coverage'] = update.pop('history_coverage')
            stock['daily_update'] = {'target':target,**update}
            if not prior or update['reason']=='qfq_mismatch' or not stock.get('finance'):
                refresh_finance(sd,stock,finance_failures)
            counts['success']+=1
            counts['full_refreshes']+=int(update['full_refresh'])
            if update['reason']:
                counts['refresh_reasons'][update['reason']] = counts['refresh_reasons'].get(update['reason'],0)+1
            consecutive_network_failures=0
        except Exception as exc:
            stock['errors']=['daily_update_failed:'+type(exc).__name__+':'+str(exc)]
            counts['failed']+=1
            consecutive_network_failures = consecutive_network_failures+1 if '三个入口均不可用' in str(exc) else 0
        entry = store.checkpoint(run_key,stock,prior['revision'] if prior else None,old)
        entries.append(entry)
        event = {'symbol':sid,'status':'failed' if stock['errors'] else 'saved','daily_update':stock.get('daily_update'),
                 'elapsed_seconds':round(time.perf_counter()-started,3),'request_metrics':sd.V39_CLIENT.metrics()}
        with (run_dir/'collection-events.jsonl').open('a') as log:
            log.write(json.dumps(event,ensure_ascii=False,separators=(',',':'))+'\n')
        if index%50==0 or index==len(universe):
            atomic_json(run_dir/'progress.json',{'completed':index,**counts,'elapsed_seconds':round(time.perf_counter()-started,3),
                        'request_metrics':sd.V39_CLIENT.metrics()})
        if consecutive_network_failures>=5:
            raise RuntimeError('five_consecutive_market_network_failures; checkpoints retained')
    pool_sources={s.get('universe_source','baostock current ACTIVE SH/SZ A-share pool') for s in universe}
    if len(pool_sources)!=1:
        raise ValueError('mixed_universe_sources')
    header.update(target_date=target,market_days=calendar,
                  scope='subset' if main_board_only else 'current_active_sh_sz',
                  universe_filter='MAIN' if main_board_only else None,
                  universe_source=next(iter(pool_sources))+('; user-requested SH/SZ MAIN filter' if main_board_only else ''),
                  collection_request={**header.get('collection_request',{}),'start_date':start,'end_date':target,'adjust':'qfq'})
    header['sources']={**header.get('sources',{}),'finance':'Cached disclosed total shares with per-stock as_of; refreshed for new members and detected qfq changes'}
    snapshot_id=store.save_snapshot(header,entries)
    return snapshot_id,{'preparation_seconds':preparation_seconds,'collection_seconds':time.perf_counter()-started,
                        **counts,'universe_before_filter':full_pool_size,**sd.V39_CLIENT.metrics()}


def publish_latest(root, run_dir):
    temporary=root/'.latest.tmp'
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(os.path.relpath(run_dir,root))
    temporary.replace(root/'latest')


def resolve_latest(root, header, requested, offline=False, fetch_calendar=None):
    if offline:
        return header['target_date'], 0
    calendar = list(header['market_days'])
    cache = root/'_cache/calendar.json'
    if cache.exists():
        saved = json.loads(cache.read_text())
        calendar = sorted(set(calendar + saved['days']))
    requests = 0
    if requested > max(calendar):
        if fetch_calendar is None:
            import data_adapter as sd
            before = sd.V39_CLIENT.requests
            added = calendar_range(sd,date.fromisoformat(max(calendar))+timedelta(days=1),date.fromisoformat(requested))
            requests = sd.V39_CLIENT.requests-before
        else:
            added = fetch_calendar(date.fromisoformat(max(calendar))+timedelta(days=1),date.fromisoformat(requested))
        calendar = sorted(set(calendar + added))
        atomic_json(cache,{'source':header['calendar_source'],'days':calendar,'as_of':datetime.now(timezone.utc).isoformat()})
    return max((d for d in calendar if d<=requested),default=header['target_date']),requests


def execute(args):
    root=args.root.resolve();started=time.perf_counter();store=Store(root/'_cache/market.sqlite')
    selected=None if args.policies=='all' else args.policies.split(',')
    chosen=list(DEFAULT_POLICIES if selected is None else selected)
    wanted_versions={pid:POLICY_VERSIONS[pid] for pid in chosen}
    main_board_only=getattr(args,'main_board_only',False)
    metrics={'network_requests':0,'mode':'offline' if args.offline or args.import_input else 'daily'}
    try:
        imported_id=None
        if args.import_input:
            phase=time.perf_counter();bundle,_=load_input(args.import_input)
            if bundle.get('scope') not in ('subset','current_active_sh_sz'):
                raise ValueError('unsupported_import_scope')
            if args.date!='latest' and bundle['target_date']!=args.date:
                raise ValueError('import_target_mismatch')
            if main_board_only:
                bundle['stocks']=[s for s in bundle['stocks'] if is_main_board(symbol(s['symbol']))]
                bundle.update(scope='subset',universe_filter='MAIN')
                bundle['universe_source']+='; user-requested SH/SZ MAIN filter'
            imported_id=store.import_bundle(bundle);target=bundle['target_date']
            del bundle
            metrics['import_seconds']=time.perf_counter()-phase
        else:
            now=datetime.now(CN)
            target=(now.date() if now.time()>=wall_time(15) else now.date()-timedelta(days=1)).isoformat() if args.date=='latest' else args.date
            latest=store.latest()
            if args.date=='latest' and latest:
                header,_=store.snapshot(latest)
                target,calendar_requests=resolve_latest(root,header,target,args.offline)
                metrics['network_requests']+=calendar_requests
        if datetime.combine(date.fromisoformat(target),wall_time(15),CN)>datetime.now(CN):
            raise ValueError('target_not_yet_closed')
        default_run=root/target
        if (not args.run_dir and (default_run/'result/results.json').exists()
                and not (default_run/'input-manifest.json').exists()):
            default_run=default_run/'optimized'
        if not args.run_dir and (default_run/'result/results.json').exists():
            saved=json.loads((default_run/'result/results.json').read_text())
            previous_versions=saved_policy_versions(saved)
            if {pid:previous_versions.get(pid) for pid in chosen}!=wanted_versions:
                suffix=hashlib.sha256(json.dumps(wanted_versions,sort_keys=True).encode()).hexdigest()[:8]
                default_run=default_run/('formula-'+suffix)
        run_dir=(args.run_dir or default_run).resolve();run_dir.mkdir(parents=True,exist_ok=True)
        manifest=run_dir/'input-manifest.json'
        if manifest.exists():
            if args.retry_failures:
                raise ValueError('snapshot_already_published; immutable snapshots cannot be retried in place')
            value=json.loads(manifest.read_text())
            if value['target_date']!=target:
                raise ValueError('existing_run_date_mismatch')
            snapshot_id=value['snapshot_id']
            saved_header,_=store.snapshot(snapshot_id)
            if (saved_header.get('universe_filter')=='MAIN') != main_board_only:
                raise ValueError('existing_run_pool_filter_differ; use a separate --run-dir')
        elif imported_id:
            snapshot_id=imported_id
        else:
            scope='subset' if main_board_only else 'current_active_sh_sz'
            candidates=store.db.execute('SELECT id FROM snapshots WHERE day=? AND scope=? ORDER BY rowid DESC',(target,scope)).fetchall()
            same=next((s for s in candidates if (store.snapshot(s[0])[0].get('universe_filter')=='MAIN')==main_board_only),None)
            if same:
                snapshot_id=same[0]
            else:
                if args.offline:
                    raise ValueError('offline_target_snapshot_unavailable')
                prior_id=store.latest(before=target)
                if not prior_id:
                    # Bootstrap once; fetch_data checkpoints stock files and batch quotes.
                    bootstrap=root/'_bootstrap'/target/('main-board-input.json' if main_board_only else 'input.json');bootstrap.parent.mkdir(parents=True,exist_ok=True)
                    if not bootstrap.exists():
                        command=[sys.executable,str(Path(__file__).with_name('fetch_data.py')),'--date',target,'--out',str(bootstrap)]
                        if main_board_only:
                            command.append('--main-board-only')
                        subprocess.run(command,check=True)
                    bundle,_=load_input(bootstrap);snapshot_id=store.import_bundle(bundle);del bundle
                    metrics['mode']='bootstrap'
                    metrics['network_requests']=None
                else:
                    snapshot_id,collected=collect(store,prior_id,target,run_dir,args.request_interval,args.retry_failures,main_board_only)
                    metrics.update(collected);metrics['network_requests']=collected['http_requests']
        store.manifest(snapshot_id,manifest)
        out=run_dir/'result'
        if (out/'results.json').exists():
            existing=json.loads((out/'results.json').read_text())
            previous_versions=saved_policy_versions(existing)
            if {pid:previous_versions.get(pid) for pid in chosen}!=wanted_versions:
                raise FileExistsError('existing_result_formula_differ; use --run-dir for a separate calculation')
            if ([p['id'] for p in existing['policies']]!=chosen or existing.get('options')!={'yellow_filter':args.yellow_filter,'require_pullback':args.require_pullback}):
                raise FileExistsError('existing_result_options_differ; use --run-dir for a separate calculation')
            _,digest=load_input(manifest,set())
            if existing.get('input_sha256')!=digest:
                raise ValueError('existing_result_input_mismatch')
            if not (out/'report.html').exists():
                rebuild_report(out)
            result={'out':str(out),'reused':True,'policies':[{k:p[k] for k in ('id','status','pool','evaluated','missing','hit_count')} for p in existing['policies']]}
        else:
            result=run(manifest,out,selected,args.yellow_filter,args.require_pullback)
        publish_latest(root,run_dir)
        metrics['total_seconds']=time.perf_counter()-started
        metrics['status']='completed'
        metrics['screen']=result.get('timings',{})
        # Preserve each invocation instead of replacing the first collection statistics.
        with (run_dir/'run-events.jsonl').open('a') as log:
            log.write(json.dumps(metrics,ensure_ascii=False,separators=(',',':'))+'\n')
        if not (run_dir/'run-metrics.json').exists():
            atomic_json(run_dir/'run-metrics.json',metrics)
        return {**result,'target_date':target,'run_metrics':metrics,'manifest':str(manifest)}
    except Exception as exc:
        metrics.update(status='failed',error=type(exc).__name__+':'+str(exc),total_seconds=time.perf_counter()-started)
        if 'run_dir' in locals():
            with (run_dir/'run-events.jsonl').open('a') as log:
                log.write(json.dumps(metrics,ensure_ascii=False,separators=(',',':'))+'\n')
            atomic_json(run_dir/'run-failure.json',metrics)
        raise
    finally:
        store.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--date',default='latest')
    parser.add_argument('--import-input',type=Path,help='Import a legacy snapshot and run without market requests')
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--run-dir',type=Path)
    parser.add_argument('--policies',default='all',help='Default/all: B1,B1_WEEKLY,MA_GOLDEN_CROSS_FIRST_PULLBACK; other IDs require explicit selection')
    parser.add_argument('--yellow-filter',action='store_true')
    parser.add_argument('--require-pullback',action='store_true')
    parser.add_argument('--main-board-only',action='store_true',help='User-requested SH/SZ MAIN universe; exclude other boards before collection')
    parser.add_argument('--request-interval',type=float,default=0.25)
    parser.add_argument('--retry-failures',action='store_true',help='Retry failed collection checkpoints before snapshot publication')
    args=parser.parse_args()
    if 'B1_黄线上' in args.policies.split(','):
        args.policies=','.join('B1' if p=='B1_黄线上' else p for p in args.policies.split(','))
        args.yellow_filter=True
    if args.request_interval<0.12:
        parser.error('request interval must be at least 0.12 seconds')
    selected=list(DEFAULT_POLICIES) if args.policies=='all' else args.policies.split(',')
    if not selected or len(selected)!=len(set(selected)) or any(p not in POLICIES for p in selected):
        parser.error('unknown_or_duplicate_policy; supported: '+','.join(POLICIES))
    try:
        with run_lock(args.root):
            result=execute(args)
        print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        raise SystemExit('Daily screening failed: '+type(exc).__name__+':'+str(exc))


if __name__=='__main__':
    main()
