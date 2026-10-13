"""Run the current three default policies on a provenance-bearing input bundle."""
import argparse
import hashlib
import json
import math
import re
import os
import time as monotonic_time
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from policies import (B1_PARAMETERS, DEFAULT_POLICIES, POLICIES, POLICY_VERSIONS,
                      aggregate_iso_weeks, run_policy, validate)

CN = ZoneInfo('Asia/Shanghai')
PREFIX = {'sh': ('600', '601', '603', '605', '688', '689'), 'sz': ('000', '001', '002', '003', '300', '301')}


def symbol(value):
    value = str(value).strip().lower().replace('.', '')
    if re.fullmatch(r'\d{6}(sh|sz)', value):
        value = value[-2:] + value[:6]
    if re.fullmatch(r'\d{6}', value):
        value = ('sh' if value.startswith(PREFIX['sh']) else 'sz') + value
    if not re.fullmatch(r'(sh|sz)\d{6}', value) or not value[2:].startswith(PREFIX[value[:2]]):
        raise ValueError('unsupported_sh_sz_A_share:' + value)
    return value


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    tmp.replace(path)


def finite(v):
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def clean_numbers(value):
    if isinstance(value,float) and not math.isfinite(value):
        return None
    if isinstance(value,list):
        return [clean_numbers(v) for v in value]
    if isinstance(value,dict):
        return {k:clean_numbers(v) for k,v in value.items()}
    return value


def prepare_stock(stock, target, calendar):
    """Keep a failed member in the denominator. Never substitute a missing bar."""
    stock = clean_numbers(stock)
    out = dict(stock)
    errors = list(stock.get('errors', []))
    rows = stock.get('bars', [])
    dates_seen = [r.get('date') for r in rows if isinstance(r,dict) and isinstance(r.get('date'),str) and r['date'] <= target] if isinstance(rows,list) else []
    last_seen = max(dates_seen,default=None)
    try:
        if stock.get('asset_status') != 'ACTIVE':
            raise ValueError('active_status_not_confirmed')
        if stock.get('adjust') != 'qfq' or not stock.get('bar_source'):
            raise ValueError('missing_consistent_qfq_provenance')
        stamp = datetime.fromisoformat(stock['bars_as_of'].replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.astimezone(CN) < datetime.combine(datetime.fromisoformat(target).date(), time(15), CN):
            raise ValueError('not_confirmed_after_close')
        rows = [dict(r) for r in rows if r['date'] <= target]
        validate(rows, len(rows) or 1, target)
        if any(r['date'] not in calendar for r in rows):
            raise ValueError('bar_on_nonmarket_day')
        missing = calendar.intersection(d for d in calendar if rows[0]['date'] <= d <= target) - {r['date'] for r in rows}
        verified = stock.get('verified_suspensions', [])
        if verified and not stock.get('suspension_source'):
            raise ValueError('missing_suspension_evidence')
        unexplained = sorted(missing - set(verified))
        if unexplained:
            raise ValueError('unexplained_missing_bars:' + ','.join(unexplained[:8]))
        unit = stock.get('volume_unit')
        if unit == 'shares':
            rows = [{**r, 'volume': r['volume'] / 100} for r in rows]
        elif unit != 'lots':
            raise ValueError('unverified_volume_unit')
        if not stock.get('volume_unit_source'):
            raise ValueError('missing_volume_unit_provenance')
    except (KeyError, ValueError, TypeError) as exc:
        errors.append(str(exc))
    out.update(bars=[] if errors else rows, last_bar_date_seen=last_seen, errors=sorted(set(errors)), volume_unit='lots' if not errors else stock.get('volume_unit'))
    return out


def evaluate(bundle, policy_ids=None, yellow_filter=False, require_pullback=False, retain_all_bars=True):
    target = bundle['target_date']
    datetime.strptime(target, '%Y-%m-%d')
    if bundle.get('schema_version') != '1.0':
        raise ValueError('unsupported_input_schema')
    if not bundle.get('calendar_source') or not bundle.get('universe_source'):
        raise ValueError('missing_calendar_or_universe_source')
    if bundle.get('scope') not in ('subset', 'current_active_sh_sz', 'point_in_time'):
        raise ValueError('scope_must_be_explicit')
    if bundle['scope'] == 'point_in_time':
        raise ValueError('historical_backtest_not_implemented: obtain point-in-time pool, finance and boards separately')
    calendar = set(bundle['market_days'])
    for day in calendar:
        datetime.strptime(day, '%Y-%m-%d')
    if target not in calendar:
        raise ValueError('target_not_confirmed_market_day')
    prior = max((d for d in calendar if d < target), default=None)
    if not prior:
        raise ValueError('previous_market_day_unavailable')
    selected = list(DEFAULT_POLICIES if policy_ids is None else policy_ids)
    if not selected or len(selected) != len(set(selected)) or any(p not in POLICIES for p in selected):
        raise ValueError('unknown_or_duplicate_policy')
    stocks = {}
    rows_by_policy = {pid: [] for pid in selected}
    for item in bundle['stocks']:
        sid = symbol(item['symbol'])
        if sid in stocks:
            raise ValueError('duplicate_stock:' + sid)
        stock = prepare_stock({**item, 'symbol':sid}, target, calendar)
        weeks = aggregate_iso_weeks(stock['bars']) if 'B1_WEEKLY' in selected and not stock['errors'] else None
        hit = False
        for pid in selected:
            if stock['errors']:
                value = {'status':'not_evaluated', 'reasons':stock['errors']}
            else:
                finance, boards = stock.get('finance'), stock.get('boards')
                finance = finance if isinstance(finance,dict) and finance.get('source') else None
                boards = boards if isinstance(boards,dict) and boards.get('source') else None
                value = run_policy(pid, stock['bars'], target, finance=finance, boards=boards,
                                   yellow_filter=yellow_filter, require_pullback=require_pullback,
                                   _validated=True, _weeks=weeks)
            rows_by_policy[pid].append({'symbol':sid, 'actual_last_bar_date':stock['last_bar_date_seen'], **value})
            hit = hit or value['status'] == 'hit'
        if weeks is not None and hit:
            stock['_weeks'] = weeks
        if not retain_all_bars and not hit:
            stock['bars'] = []
        stocks[sid] = stock
    if not stocks:
        raise ValueError('empty_or_unavailable_universe')
    reports = []
    for pid, rows in rows_by_policy.items():
        evaluated = sum(r['status'] in ('hit','not_hit') for r in rows)
        missing = len(rows)-evaluated
        hits = [r['symbol'] for r in rows if r['status']=='hit']
        if pid == 'B1':
            scores = {r['symbol']:r['metrics']['shape_score'] for r in rows if r['status']=='hit'}
            hits.sort(key=lambda sid: (-scores[sid], sid))
        reports.append({'id':pid, 'version':POLICY_VERSIONS[pid],
                        'parameters':dict(B1_PARAMETERS) if pid=='B1' else {},
                        'status':'completed' if not missing else 'partial' if evaluated else 'not_evaluated',
                        'pool':len(rows), 'evaluated':evaluated, 'missing':missing,
                        'hit_count':sum(r['status']=='hit' for r in rows), 'results':rows,
                        'hits':hits})
    return {'schema_version':'1.0', 'target_date':target, 'previous_market_day':prior,
            'generated_at':datetime.now(timezone.utc).isoformat(), 'scope':bundle['scope'],
            'universe_source':bundle['universe_source'], 'calendar_source':bundle['calendar_source'],
            'sources':bundle.get('sources',{}), 'stocks':stocks, 'policies':reports,
            'policy_versions':{pid:POLICY_VERSIONS[pid] for pid in selected},
            'options':{'yellow_filter':yellow_filter, 'require_pullback':require_pullback},
            'formula_basis':'B1 uses user-defined '+POLICY_VERSIONS['B1']+'; other formulas based on Go commit 5ed70450; independent Python implementation; B2 removed'}


def save_results(report, out, input_path, digest):
    from storage import atomic_json
    out = Path(out)
    compact = {**report, 'schema_version':'2.0', 'input_sha256':digest,
               'input_ref':os.path.relpath(Path(input_path).resolve(), out.resolve()),
               'stocks':{sid:{k:v for k,v in stock.items() if k not in ('bars','_weeks')}
                         for sid,stock in report['stocks'].items()}}
    atomic_json(out/'results.json', compact)
    for policy in report['policies']:
        if policy['id']=='B1_V2' and policy['status']=='completed':
            atomic_json(out/'B1_V2.snapshot.json', {'policy_id':'B1_V2', 'version':'b1_yellow_white_v2.0',
                        'status':'success', 'trade_date':report['target_date'], 'saved_at':report['generated_at'],
                        'scope':report['scope'], 'input_sha256':digest,
                        'candidates':[{'symbol':sid,'signal_date':report['stocks'][sid]['bars'][-1]['date']}
                                      for sid in policy['hits']]})


def rebuild_report(out):
    from storage import load_input
    from reporting import write_reports
    out = Path(out)
    report = json.loads((out/'results.json').read_text())
    symbols = {sid for policy in report['policies'] for sid in policy['hits']}
    bundle, digest = load_input(out/report['input_ref'], symbols)
    if digest != report['input_sha256']:
        raise ValueError('input_digest_mismatch')
    calendar = set(bundle['market_days'])
    for item in bundle['stocks']:
        stock = prepare_stock(item, report['target_date'], calendar)
        if stock['errors']:
            raise ValueError('candidate_data_changed:' + item['symbol'])
        report['stocks'][item['symbol']] = stock
    write_reports(report, out)


def run(input_path, out, selected=None, yellow_filter=False, require_pullback=False):
    from storage import load_input, atomic_json
    from reporting import write_reports
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if (out/'results.json').exists():
        raise FileExistsError('Output contains a completed calculation; use --rebuild-report or a new directory')
    timings = {}
    started = monotonic_time.perf_counter()
    bundle, digest = load_input(input_path)
    timings['input_seconds'] = monotonic_time.perf_counter()-started
    phase = monotonic_time.perf_counter()
    report = evaluate(bundle, selected, yellow_filter, require_pullback, retain_all_bars=False)
    timings['evaluate_seconds'] = monotonic_time.perf_counter()-phase
    phase = monotonic_time.perf_counter()
    save_results(report, out, input_path, digest)
    timings['result_save_seconds'] = monotonic_time.perf_counter()-phase
    atomic_json(out/'calculation-status.json', {'status':'saved', 'target_date':report['target_date'], 'timings':timings})
    phase = monotonic_time.perf_counter()
    try:
        write_reports(report, out)
    except Exception as exc:
        atomic_json(out/'report-failure.json', {'status':'report_failed', 'calculation_status':'saved',
                    'error':type(exc).__name__+':'+str(exc)})
        raise
    timings['report_seconds'] = monotonic_time.perf_counter()-phase
    timings['total_seconds'] = monotonic_time.perf_counter()-started
    atomic_json(out/'timings.json', timings)
    return {'out':str(out.resolve()), 'timings':timings,
            'policies':[{k:p[k] for k in ('id','status','pool','evaluated','missing','hit_count')} for p in report['policies']]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--policies', default='all', help='Default/all: B1,B1_WEEKLY,MA_GOLDEN_CROSS_FIRST_PULLBACK; other IDs require explicit selection')
    parser.add_argument('--yellow-filter', action='store_true')
    parser.add_argument('--require-pullback', action='store_true')
    parser.add_argument('--rebuild-report', action='store_true', help='Rebuild saved results without network or recomputing policies')
    args = parser.parse_args()
    if args.rebuild_report:
        rebuild_report(args.out)
        print(json.dumps({'out':str(args.out.resolve()),'network_requests':0,'calculation_reused':True}))
        return
    if not args.input:
        parser.error('--input is required unless --rebuild-report is used')
    selected = None if args.policies=='all' else args.policies.split(',')
    if selected and 'B1_黄线上' in selected:
        selected = ['B1' if p=='B1_黄线上' else p for p in selected]
        args.yellow_filter = True
    try:
        result = run(args.input, args.out, selected, args.yellow_filter, args.require_pullback)
        print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        dump(args.out/'failure.json', {'status':'failed','error':type(exc).__name__+':'+str(exc)})
        raise SystemExit('Task failed; see failure.json: '+str(exc))


if __name__=='__main__':
    main()
