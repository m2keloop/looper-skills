"""Run seven policies on a provenance-bearing input bundle. Python standard library only."""
import argparse
import hashlib
import json
import math
import re
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from policies import POLICIES, run_b2, run_policy, validate

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


def evaluate(bundle, policy_ids=None, yellow_filter=False, require_pullback=False, snapshot=None, strict_b2=False):
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
    selected = list(policy_ids or POLICIES)
    if len(selected) != len(set(selected)) or any(p not in POLICIES for p in selected):
        raise ValueError('unknown_or_duplicate_policy')
    stocks = {}
    for item in bundle['stocks']:
        sid = symbol(item['symbol'])
        if sid in stocks:
            raise ValueError('duplicate_stock:' + sid)
        stocks[sid] = prepare_stock({**item, 'symbol': sid}, target, calendar)
    if not stocks:
        raise ValueError('empty_or_unavailable_universe')
    reports = []
    for pid in selected:
        if pid == 'B2_V1':
            valid_data = {sid: s['bars'] for sid, s in stocks.items() if not s['errors']}
            b2 = run_b2(snapshot, valid_data, target, prior, strict_next_day=strict_b2)
            if b2['status'] == 'source_unavailable':
                reports.append({'id': pid, 'status': 'source_unavailable', 'reason': b2['reason'], 'pool': None,
                                'evaluated': 0, 'missing': None, 'hit_count': None, 'results': [], 'hits': []})
                continue
            rows = b2['results']
            for row in rows:
                source_stock = stocks.get(row['symbol'])
                if source_stock and source_stock['errors']:
                    row.update(status='not_evaluated', reasons=source_stock['errors'])
        else:
            rows = []
            for sid, stock in stocks.items():
                if stock['errors']:
                    value = {'status': 'not_evaluated', 'reasons': stock['errors']}
                else:
                    finance = stock.get('finance')
                    finance = finance if isinstance(finance,dict) and finance.get('source') else None
                    boards = stock.get('boards')
                    boards = boards if isinstance(boards,dict) and boards.get('source') else None
                    value = run_policy(pid, stock['bars'], target, finance=finance, boards=boards,
                                       yellow_filter=yellow_filter, require_pullback=require_pullback)
                rows.append({'symbol': sid, 'actual_last_bar_date': stock['last_bar_date_seen'], **value})
        evaluated = sum(r['status'] in ('hit', 'not_hit') for r in rows)
        missing = len(rows) - evaluated
        reports.append({'id': pid, 'status': 'completed' if not missing else 'partial' if evaluated else 'not_evaluated',
                        'pool': len(rows), 'evaluated': evaluated, 'missing': missing,
                        'hit_count': sum(r['status'] == 'hit' for r in rows), 'results': rows,
                        'hits': [r['symbol'] for r in rows if r['status'] == 'hit']})
    return {'schema_version': '1.0', 'target_date': target, 'previous_market_day': prior,
            'generated_at': datetime.now(timezone.utc).isoformat(), 'scope': bundle['scope'],
            'universe_source': bundle['universe_source'], 'calendar_source': bundle['calendar_source'],
            'sources': bundle.get('sources', {}), 'stocks': stocks, 'policies': reports,
            'options': {'yellow_filter': yellow_filter, 'require_pullback': require_pullback, 'strict_b2_next_day': strict_b2},
            'formula_basis': 'user policy document based on Go commit 5ed70450; independent Python implementation'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--policies', default='all', help='Comma-separated policy IDs; default all seven')
    parser.add_argument('--yellow-filter', action='store_true')
    parser.add_argument('--require-pullback', action='store_true')
    parser.add_argument('--strict-b2-next-day', action='store_true')
    parser.add_argument('--b2-snapshot', type=Path, help='Pre-existing persisted source snapshot; never synthesize a missing one')
    args = parser.parse_args()
    # Preserve completed runs and genuine saved snapshots rather than overwrite them.
    args.out.mkdir(parents=True, exist_ok=True)
    if any((args.out / n).exists() for n in ['results.json', 'B1_V2.snapshot.json']):
        parser.error('Output directory contains a completed run; use a new run directory')
    try:
        bundle = json.loads(args.input.read_text())
        snapshot = None
        snapshot_error = None
        if args.b2_snapshot:
            try:
                snapshot = json.loads(args.b2_snapshot.read_text())
            except (OSError,ValueError) as exc:
                snapshot_error = type(exc).__name__ + ':source_snapshot_unreadable'
        selected = None if args.policies == 'all' else args.policies.split(',')
        if selected and 'B1_黄线上' in selected:
            selected = ['B1' if p == 'B1_黄线上' else p for p in selected]
            args.yellow_filter = True
        report = evaluate(bundle, selected, args.yellow_filter, args.require_pullback, snapshot, args.strict_b2_next_day)
        report['input_sha256'] = hashlib.sha256(args.input.read_bytes()).hexdigest()
        if snapshot_error:
            report['b2_source_error'] = snapshot_error
        elif args.b2_snapshot:
            report['b2_source_sha256'] = hashlib.sha256(args.b2_snapshot.read_bytes()).hexdigest()
        dump(args.out / 'results.json', report)
        for policy in report['policies']:
            if policy['id'] == 'B1_V2' and policy['status'] == 'completed':
                dump(args.out / 'B1_V2.snapshot.json', {'policy_id': 'B1_V2', 'version': 'b1_yellow_white_v2.0',
                     'status': 'success', 'trade_date': report['target_date'], 'saved_at': report['generated_at'],
                     'scope': report['scope'], 'input_sha256': report['input_sha256'],
                     'candidates': [{'symbol': sid, 'signal_date': report['stocks'][sid]['bars'][-1]['date']} for sid in policy['hits']]})
        from reporting import write_reports
        write_reports(report, args.out)
        print(json.dumps({'out': str(args.out.resolve()), 'policies': [{k: p[k] for k in ('id','status','pool','evaluated','missing','hit_count')} for p in report['policies']]}, ensure_ascii=False))
    except Exception as exc:
        dump(args.out / 'failure.json', {'status': 'failed', 'error': type(exc).__name__ + ': ' + str(exc)})
        raise SystemExit('Task failed; see failure.json: ' + str(exc))


if __name__ == '__main__':
    main()
