#!/usr/bin/env python3
"""Collect all capital-tier public holdings for one hot-contest snapshot."""
import argparse
import fcntl
import hashlib
import json
import math
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

from analyze_hot_contests import read_snapshot
from collect_hot_contests import AccessLimited, PublicClient, SourceError, now, official_url, save, source_data

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS_PATH = ROOT / 'data' / 'consensus-weights.json'
GROUPS = {'1': '新手组', '10': '十万组', '100': '百万组', '1000': '千万组'}
SCOPE = '新手组、十万组、百万组、千万组公开可读取持仓，仅匹配各赛本次官方热榜股票'


def weights_config():
    config = json.loads(WEIGHTS_PATH.read_text(encoding='utf-8'))
    weights = config.get('weights')
    if (not isinstance(config.get('version'), str) or not config['version']
            or not isinstance(weights, dict) or set(weights) != set(GROUPS)
            or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in weights.values())
            or len(set(weights.values())) != len(GROUPS)
            or weights['1000'] != max(weights.values())):
        raise SourceError('Invalid consensus-weights configuration')
    return {'weight_version': config['version'], 'weights': weights,
            'labels': config.get('labels', GROUPS), 'formula': config.get('formula')}


def number(value, name):
    if not re.fullmatch(r'\d+', str(value)):
        raise SourceError('Group ranking has no valid ' + name)
    return int(value)


def stock_map(rows):
    result = {}
    for row in rows:
        code, market = row['stock_code'], row['market']
        if code in result and result[code] != market:
            raise SourceError('Official hot-list code maps to multiple markets; refusing to guess')
        result[code] = market
    return result


def snapshot_identity(snapshot):
    digest = hashlib.sha256()
    for filename in ('manifest.json', 'catalog.json', 'all-hot-stocks.json'):
        digest.update((snapshot['path'] / filename).read_bytes())
    return {'schema_version': 1, 'base_run_id': snapshot['manifest']['run_id'],
            'base_completed_at': snapshot['manifest']['completed_at'],
            'base_fingerprint': digest.hexdigest(), 'collection_date': now()[:10]}


def load_checkpoint(snapshot, code):
    path = snapshot['path'] / f'group-{code}-checkpoint.json'
    identity = {**snapshot_identity(snapshot), 'group_code': code}
    if path.exists():
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('metadata') == identity and isinstance(saved.get('contests'), dict):
            return path, saved
    return path, {'metadata': identity, 'contests': {}, 'request_log': []}


def collect_roster(client, contest, entry, code, raw_dir, checkpoint_path, checkpoint):
    seen = set(entry.get('members', []))
    page = int(entry.get('last_page', 0)) + 1
    entry.setdefault('members', [])
    entry.setdefault('holdings', {})
    while True:
        if page > contest['participant_count'] + 1:
            raise SourceError('Group ranking exceeded participant-count safety bound')
        payload = client.request(official_url('match_ranking_by_share', contest['contest_key'],
                                               month='', sort_type='', sort_order=0,
                                               group_type=code, page=page))
        data = source_data(payload)
        if number(data.get('page'), 'page number') != page or data.get('article_title') != contest['competition_name']:
            raise SourceError('Group ranking belongs to another page or contest')
        rows = data.get('rank_list')
        if not isinstance(rows, list):
            raise SourceError('Group ranking has no rank_list')
        save(raw_dir / f'ranking-page-{page:04d}.json', {
            'page': page, 'article_title': contest['competition_name'],
            'rank_list': [{'page_index': row.get('page_index'), 'tag': row.get('tag'),
                           'user_key_sha256': hashlib.sha256(str(row.get('user_key', '')).encode('utf-8')).hexdigest()}
                          for row in rows if isinstance(row, dict)]})
        if not rows:
            entry.update(ranking_complete=True, ranking_status='complete', ranking_checked_at=now())
            save(checkpoint_path, checkpoint)
            return
        start_count = len(entry['members'])
        page_members = []
        for offset, row in enumerate(rows, 1):
            if not isinstance(row, dict) or str(row.get('tag')) != code:
                raise SourceError('Group ranking contains a member outside the requested capital group')
            uid = row.get('user_key')
            if not isinstance(uid, str) or not uid or uid in seen:
                raise SourceError('Group ranking repeats a member or has no public user key')
            if number(row.get('page_index'), 'page_index') != start_count + offset:
                raise SourceError('Group ranking page_index is not continuous')
            page_members.append(uid)
            seen.add(uid)
        entry['members'].extend(page_members)
        if len(entry['members']) > contest['participant_count']:
            raise SourceError('Capital-group member count exceeds total contest participant count')
        entry.update(last_page=page, ranking_checked_at=now())
        save(checkpoint_path, checkpoint)
        print(f'{GROUPS[code]} roster: {contest["competition_name"]}; {len(entry["members"])} members', flush=True)
        page += 1


def collect_position(client, contest_key, uid, mapping, raw_dir):
    payload = client.request(official_url('position_by_share', contest_key, user_key=uid))
    filename = hashlib.sha256(uid.encode('utf-8')).hexdigest() + '.json'
    positions = source_data(payload).get('position_list')
    if not isinstance(positions, list):
        raise SourceError('Public response has no position_list; holdings remain unknown')
    codes = set()
    for row in positions:
        if not isinstance(row, dict) or not isinstance(row.get('stock_code'), str) or not row['stock_code']:
            raise SourceError('Public position row has no valid stock code')
        if row['stock_code'] in mapping:
            codes.add(row['stock_code'])
    result = {'status': 'success', 'stock_codes': sorted(codes),
              'public_position_list_empty': not positions, 'fetched_at': now()}
    save(raw_dir / 'positions' / filename, {
        'matched_stock_codes': result['stock_codes'],
        'public_position_list_empty': result['public_position_list_empty'],
        'fetched_at': result['fetched_at']})
    return result


def validate_entry(entry, total_participants):
    members = entry.get('members', [])
    holdings = entry.get('holdings', {})
    if (not isinstance(members, list) or any(not isinstance(uid, str) or not uid for uid in members)
            or len(members) != len(set(members)) or len(members) > total_participants
            or not isinstance(holdings, dict) or set(holdings) - set(members)):
        raise SourceError('Invalid group checkpoint')
    for value in holdings.values():
        if value.get('status') not in ('success', 'failed'):
            raise SourceError('Invalid group position checkpoint')
        codes = value.get('stock_codes', [])
        if value['status'] == 'success' and (not isinstance(codes, list) or len(codes) != len(set(codes))):
            raise SourceError('Invalid group stock-code checkpoint')


def read_existing_thousand(snapshot):
    path = snapshot['path'] / 'thousand-group.json'
    if not path.exists():
        raise SourceError('Complete thousand-group collection is required before weighting all capital tiers')
    payload = json.loads(path.read_text(encoding='utf-8'))
    meta = payload.get('metadata', {})
    if meta.get('base_run_id') != snapshot['manifest']['run_id'] or meta.get('collection_complete') is not True:
        raise SourceError('Thousand-group collection is incomplete or belongs to another snapshot')
    return {'metadata': {'group_code': '1000', 'group_name': GROUPS['1000'],
                         'base_run_id': snapshot['manifest']['run_id'],
                         'collection_complete': True, 'member_count': meta['member_count'],
                         'holdings_success_count': meta['holdings_success_count'],
                         'holdings_failed_count': meta['holdings_failed_count'],
                         'completed_at': meta['completed_at'], 'scope': meta['scope']},
            'contests': payload['contests'],
            'records': [{**row, 'group_holder_count': row['thousand_holder_count']}
                        for row in payload['records']]}


def collect_group(snapshot, code, client, interval):
    client.events = []
    checkpoint_path, checkpoint = load_checkpoint(snapshot, code)
    contests = [c for c in snapshot['contests'] if c['contest_key'] in snapshot['selected']]
    mappings = {c['contest_key']: stock_map([r for r in snapshot['rows'] if r['contest_key'] == c['contest_key']])
                for c in contests}
    for c in contests:
        checkpoint['contests'].setdefault(c['contest_key'], {
            'members': [], 'holdings': {}, 'ranking_complete': False, 'ranking_status': 'pending'})
        validate_entry(checkpoint['contests'][c['contest_key']], c['participant_count'])
    save(checkpoint_path, checkpoint)
    stopped = False
    try:
        for contest in contests:
            entry = checkpoint['contests'][contest['contest_key']]
            if entry.get('ranking_complete') is True:
                continue
            raw = snapshot['path'] / 'competitions' / contest['contest_key'] / 'raw' / f'group-{code}'
            try:
                collect_roster(client, contest, entry, code, raw, checkpoint_path, checkpoint)
            except AccessLimited as error:
                entry.update(ranking_status='failed', failed_stage='ranking', error=str(error))
                stopped = True
                break
            except (SourceError, OSError) as error:
                entry.update(ranking_status='failed', failed_stage='ranking', error=str(error))
                save(checkpoint_path, checkpoint)
        if not stopped:
            for contest in contests:
                key = contest['contest_key']
                entry = checkpoint['contests'][key]
                if entry.get('ranking_complete') is not True or entry.get('failed_stage'):
                    continue
                raw = snapshot['path'] / 'competitions' / key / 'raw' / f'group-{code}'
                pending = [uid for uid in entry['members'] if entry['holdings'].get(uid, {}).get('status') != 'success']
                for n, uid in enumerate(pending, 1):
                    try:
                        entry['holdings'][uid] = collect_position(client, key, uid, mappings[key], raw)
                    except AccessLimited as error:
                        entry['holdings'][uid] = {'status': 'failed', 'error': str(error), 'fetched_at': now()}
                        stopped = True
                    except (SourceError, OSError) as error:
                        entry['holdings'][uid] = {'status': 'failed', 'error': str(error), 'fetched_at': now()}
                    save(checkpoint_path, checkpoint)
                    if n % 25 == 0 or n == len(pending) or stopped:
                        done = sum(v.get('status') == 'success' for v in entry['holdings'].values())
                        print(f'{GROUPS[code]} holdings: {contest["competition_name"]}; {done}/{len(entry["members"])}', flush=True)
                    if stopped:
                        break
                if stopped:
                    break
    finally:
        checkpoint.setdefault('request_log', []).extend(client.events)
        save(checkpoint_path, checkpoint)
    return build_group_payload(snapshot, checkpoint, code, interval, stopped)


def build_group_payload(snapshot, checkpoint, code, interval, stopped):
    contests, records = [], []
    for contest in snapshot['contests']:
        key = contest['contest_key']
        if key not in snapshot['selected']:
            continue
        entry = checkpoint['contests'][key]
        success = sum(v.get('status') == 'success' for v in entry['holdings'].values())
        failed = sum(v.get('status') == 'failed' for v in entry['holdings'].values())
        complete = entry.get('ranking_complete') is True and not entry.get('failed_stage') and success == len(entry['members'])
        contests.append({'contest_key': key, 'competition_name': contest['competition_name'],
                         'member_count': len(entry['members']) if entry.get('ranking_complete') else None,
                         'observed_member_count': len(entry['members']),
                         'ranking_complete': entry.get('ranking_complete') is True,
                         'holdings_success_count': success, 'holdings_failed_count': failed,
                         'holdings_pending_count': len(entry['members']) - success - failed,
                         'collection_complete': complete, 'error': entry.get('error')})
        counts = Counter(code_ for v in entry['holdings'].values() if v.get('status') == 'success'
                         for code_ in v.get('stock_codes', []))
        for row in snapshot['rows']:
            if row['contest_key'] == key:
                records.append({'contest_key': key, 'competition_name': contest['competition_name'],
                                'market': row['market'], 'stock_code': row['stock_code'],
                                'stock_name': row['stock_name'], 'official_holder_count': row['holder_count'],
                                'group_holder_count': counts[row['stock_code']] if complete else None,
                                'collection_complete': complete})
    member_count = sum(c['member_count'] or 0 for c in contests)
    success_count = sum(c['holdings_success_count'] for c in contests)
    failed_count = sum(c['holdings_failed_count'] for c in contests)
    complete = all(c['collection_complete'] for c in contests)
    return {'metadata': {'schema_version': 1, 'group_code': code, 'group_name': GROUPS[code],
                         'base_run_id': snapshot['manifest']['run_id'],
                         'base_completed_at': snapshot['manifest']['completed_at'],
                         'collection_date': checkpoint['metadata']['collection_date'],
                         'completed_at': now(), 'collection_complete': complete,
                         'interval_seconds': interval, 'stopped_on_access_limit': stopped,
                         'contest_count': len(contests), 'record_count': len(records),
                         'member_count': member_count, 'holdings_success_count': success_count,
                         'holdings_failed_count': failed_count, 'scope': f'{GROUPS[code]}公开持仓，仅统计各赛原官方热榜股票'},
            'contests': contests, 'records': records}


def run(args):
    snapshot = read_snapshot(args.snapshot)
    weights = weights_config()
    groups = {'1000': read_existing_thousand(snapshot)}
    client = PublicClient(args.interval)
    for code in ('1', '10', '100'):
        groups[code] = collect_group(snapshot, code, client, args.interval)
        save(snapshot['path'] / f'group-{code}.json', groups[code])
        if groups[code]['metadata']['collection_complete'] is not True:
            raise SourceError(f'{GROUPS[code]} collection incomplete; weighted report will not publish')
    ordered = {code: groups[code] for code in GROUPS}
    metadata = {'schema_version': 1, **weights, 'scope': SCOPE,
                'base_run_id': snapshot['manifest']['run_id'],
                'base_completed_at': snapshot['manifest']['completed_at'],
                'collection_date': now()[:10], 'completed_at': now(),
                'collection_complete': all(g['metadata']['collection_complete'] for g in ordered.values()),
                'interval_seconds': args.interval,
                'member_count': sum(g['metadata']['member_count'] for g in ordered.values()),
                'holdings_success_count': sum(g['metadata']['holdings_success_count'] for g in ordered.values()),
                'holdings_failed_count': sum(g['metadata']['holdings_failed_count'] for g in ordered.values()),
                'selected_contest_count': len(snapshot['selected']),
                'completed_contest_count': min((sum(c['collection_complete'] for c in g['contests'])
                                                for g in ordered.values()), default=0),
                'weights_by_group': {code: {'label': GROUPS[code], 'weight': weights['weights'][code],
                                            'member_count': g['metadata']['member_count']}
                                     for code, g in ordered.items()}}
    if not metadata['collection_complete']:
        raise SourceError('One or more capital groups did not complete')
    result = {'metadata': metadata, 'groups': ordered}
    output = snapshot['path'] / 'group-holdings.json'
    save(output, result)
    # Keep auditable fetch logs and aggregate group counts, then remove per-user
    # checkpoints/raw responses that contain public account identifiers or
    # portfolio details which the analysis does not need.
    for code in GROUPS:
        checkpoint_path = snapshot['path'] / f'group-{code}-checkpoint.json'
        if checkpoint_path.exists():
            saved = json.loads(checkpoint_path.read_text(encoding='utf-8'))
            events = saved.get('request_log', [])
            for event in events:
                event['url'] = re.sub(r'([?&]user_key=)[^&]+', r'\1[redacted]', event.get('url', ''))
            save(snapshot['path'] / f'group-{code}-request-log.json', events)
            checkpoint_path.unlink()
        elif code == '1000':
            legacy_log = snapshot['path'] / 'thousand-group-request-log.json'
            if legacy_log.exists():
                events = json.loads(legacy_log.read_text(encoding='utf-8'))
                for event in events:
                    event['url'] = re.sub(r'([?&]user_key=)[^&]+', r'\1[redacted]', event.get('url', ''))
                save(snapshot['path'] / 'group-1000-request-log.json', events)
        for contest in snapshot['contests']:
            if contest['contest_key'] in snapshot['selected']:
                raw = snapshot['path'] / 'competitions' / contest['contest_key'] / 'raw' / f'group-{code}'
                if raw.exists():
                    shutil.rmtree(raw)
    thousand_checkpoint = snapshot['path'] / 'thousand-group-checkpoint.json'
    if thousand_checkpoint.exists():
        thousand_checkpoint.unlink()
    for contest in snapshot['contests']:
        if contest['contest_key'] in snapshot['selected']:
            raw = snapshot['path'] / 'competitions' / contest['contest_key'] / 'raw' / 'thousand-group'
            if raw.exists():
                shutil.rmtree(raw)
    print(json.dumps({'group_holdings_path': str(output), **metadata}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=2.0)
    args = parser.parse_args()
    if not 2 <= args.interval < float('inf'):
        parser.error('interval must be finite and >=2 seconds')
    args.snapshot = args.snapshot.resolve()
    try:
        with (args.snapshot / '.capital-groups.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Another capital-group collection is running; skipped.', flush=True)
                return 3
            run(args)
        return 0
    except (OSError, ValueError, TypeError, KeyError, SourceError) as error:
        print(f'Capital-group collection error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
