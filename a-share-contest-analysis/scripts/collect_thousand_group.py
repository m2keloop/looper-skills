#!/usr/bin/env python3
"""Collect the complete thousand-group public holdings for one hot-list snapshot."""
import argparse
import fcntl
import hashlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

from analyze_hot_contests import read_snapshot
from collect_hot_contests import (
    AccessLimited, PublicClient, SourceError, now, official_url, save, source_data,
)

GROUP_CODE = '1000'
WEIGHTS_PATH = Path(__file__).resolve().parents[1] / 'data' / 'consensus-weights.json'
SCOPE = '全千万组公开可读取持仓，仅统计各赛原官方热榜股票'
NOTES = [
    '千万组来自官方排名 tag=1000；按组名单逐人读取公开 position_list。',
    '公开持仓列表为空只能表示本次公开列表为空，不能证明空仓。',
    '计数仅覆盖本次原官方热榜中的股票，重复股票代码在同一成员内只计一次。',
    '千组公开持仓与官方热榜来自不同时间及观测口径，不推断其他组人数。',
    '同一成员可能参加多场比赛；跨比赛求和不代表去重投资者人数。',
]


def weight_config():
    config = json.loads(WEIGHTS_PATH.read_text(encoding='utf-8'))
    weights = config.get('weights')
    if (not isinstance(config.get('version'), str) or not config['version']
            or not isinstance(weights, dict) or set(weights) != {'100', '1000'}
            or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                   for value in weights.values())
            or len(set(weights.values())) != 2
            or weights['1000'] != max(weights.values())):
        raise SourceError('Invalid consensus-weights configuration')
    return {'weight_version': config['version'], 'weights': weights}


def integer(value, field):
    if not re.fullmatch(r'\d+', str(value)):
        raise SourceError('Group ranking has no valid ' + field)
    return int(value)


def load_checkpoint(snapshot, path):
    """Reuse only this exact base snapshot's successful work on the same day."""
    digest = hashlib.sha256()
    for filename in ('manifest.json', 'catalog.json', 'all-hot-stocks.json'):
        digest.update((snapshot['path'] / filename).read_bytes())
    identity = {
        'schema_version': 1, 'group_code': GROUP_CODE,
        'base_run_id': snapshot['manifest']['run_id'],
        'base_completed_at': snapshot['manifest']['completed_at'],
        'base_fingerprint': digest.hexdigest(), 'collection_date': now()[:10],
    }
    if path.exists():
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('metadata') == identity:
            if not isinstance(saved.get('contests'), dict):
                raise SourceError('Invalid thousand-group checkpoint')
            return saved
    return {'metadata': identity, 'contests': {}, 'request_log': []}


def stock_mapping(rows):
    mapping = {}
    for row in rows:
        code, market = row['stock_code'], row['market']
        if code in mapping and mapping[code] != market:
            raise SourceError('Official hot-list code maps to multiple markets; refusing to guess')
        mapping[code] = market
    return mapping


def validate_member_ids(entry, participant_count):
    members = entry.get('members', [])
    if (not isinstance(members, list) or any(not isinstance(uid, str) or not uid for uid in members)
            or len(members) != len(set(members)) or len(members) > participant_count):
        raise SourceError('Invalid member checkpoint')
    holdings = entry.get('holdings', {})
    if not isinstance(holdings, dict) or set(holdings) - set(members):
        raise SourceError('Invalid public-holdings checkpoint')
    for value in holdings.values():
        if not isinstance(value, dict) or value.get('status') not in ('success', 'failed'):
            raise SourceError('Invalid public-holdings checkpoint status')
        if value['status'] == 'success':
            codes = value.get('stock_codes')
            if (not isinstance(codes, list) or any(not isinstance(code, str) or not code for code in codes)
                    or len(codes) != len(set(codes))):
                raise SourceError('Invalid public-holdings checkpoint stock codes')


def collect_members(client, contest, entry, raw_dir, checkpoint, checkpoint_path):
    """The source's max_count/max_page are global, even for a filtered group."""
    entry.update(members=[], holdings={}, ranking_complete=False, ranking_status='collecting')
    entry.pop('error', None)
    entry.pop('failed_stage', None)
    seen, page = set(), 1
    while True:
        # At least one new member must be added by every nonempty page. This is
        # a safety bound, never a source-derived group page-count stop rule.
        if page > contest['participant_count'] + 1:
            raise SourceError('Group ranking exceeded participant-count safety bound')
        payload = client.request(official_url(
            'match_ranking_by_share', contest['contest_key'], month='', sort_type='',
            sort_order=0, group_type=GROUP_CODE, page=page,
        ))
        data = source_data(payload)
        actual_page = integer(data.get('page'), 'page number')
        if actual_page != page or data.get('article_title') != contest['competition_name']:
            raise SourceError('Group ranking belongs to another page or contest')
        rows = data.get('rank_list')
        if not isinstance(rows, list):
            raise SourceError('Group ranking has no rank_list')
        save(raw_dir / f'ranking-page-{page:04d}.json', {
            'page': page, 'article_title': contest['competition_name'],
            'rank_list': [{'page_index': row.get('page_index'), 'tag': row.get('tag'),
                           'user_key_sha256': hashlib.sha256(str(row.get('user_key', '')).encode('utf-8')).hexdigest()}
                          for row in rows if isinstance(row, dict)]})
        new_members = []
        for offset, row in enumerate(rows, 1):
            if not isinstance(row, dict) or str(row.get('tag')) != GROUP_CODE:
                raise SourceError('Group ranking contains a member outside the thousand group')
            uid = row.get('user_key')
            if not isinstance(uid, str) or not uid or uid in seen or uid in new_members:
                raise SourceError('Group ranking repeats a member or has no public user key')
            index = integer(row.get('page_index'), 'page_index')
            if index != len(entry['members']) + offset:
                raise SourceError('Group ranking page_index is not continuous')
            new_members.append(uid)
        if len(entry['members']) + len(new_members) > contest['participant_count']:
            raise SourceError('Thousand-group member count exceeds total contest participant count')
        entry['members'].extend(new_members)
        seen.update(new_members)
        entry.update(last_page=page, ranking_checked_at=now())
        if not rows:
            entry.update(ranking_complete=True, ranking_status='complete')
        save(checkpoint_path, checkpoint)
        print(f'Thousand-group ranking: {contest["competition_name"]}; '
              f'page {page}; {len(entry["members"])} observed members; '
              f'{entry["ranking_status"]}', flush=True)
        if not rows:
            return
        page += 1


def collect_position(client, key, uid, mapping, raw_dir):
    payload = client.request(official_url('position_by_share', key, user_key=uid))
    # The public key is retained only in the resumable checkpoint. Raw holdings
    # are reduced to stock identities needed for the aggregate.
    filename = hashlib.sha256(uid.encode('utf-8')).hexdigest() + '.json'
    data = source_data(payload)
    positions = data.get('position_list')
    if not isinstance(positions, list):
        raise SourceError('Public response has no position_list; holdings remain unknown')
    codes = set()
    for row in positions:
        if not isinstance(row, dict):
            raise SourceError('Public position row is not an object')
        code = row.get('stock_code')
        if not isinstance(code, str) or not code:
            raise SourceError('Public position stock code must preserve its string identity')
        if code in mapping:
            codes.add(code)
    result = {'status': 'success', 'stock_codes': sorted(codes),
              'public_position_list_empty': not positions, 'fetched_at': now()}
    save(raw_dir / 'positions' / filename, {
        'matched_stock_codes': result['stock_codes'],
        'public_position_list_empty': result['public_position_list_empty'],
        'fetched_at': result['fetched_at']})
    return result


def build_payload(snapshot, checkpoint, interval, stopped, config):
    contests, records, warnings = [], [], []
    for contest in snapshot['contests']:
        key = contest['contest_key']
        if key not in snapshot['selected']:
            continue
        entry = checkpoint['contests'][key]
        holdings = entry['holdings']
        success = sum(value.get('status') == 'success' for value in holdings.values())
        failed = sum(value.get('status') == 'failed' for value in holdings.values())
        ranked = entry.get('ranking_complete') is True
        complete = ranked and not entry.get('failed_stage') and success == len(entry['members'])
        summary = {
            'contest_key': key, 'competition_name': contest['competition_name'],
            'member_count': len(entry['members']) if ranked else None,
            'observed_member_count': len(entry['members']), 'ranking_complete': ranked,
            'holdings_success_count': success, 'holdings_failed_count': failed,
            'holdings_pending_count': len(entry['members']) - success - failed,
            'public_empty_list_count': sum(value.get('public_position_list_empty') is True
                                          for value in holdings.values()),
            'ranking_checked_at': entry.get('ranking_checked_at'),
            'collection_complete': complete,
            'failed_stage': entry.get('failed_stage') or ('holdings' if failed else None),
            'error': entry.get('error') or next((value.get('error') for value in holdings.values()
                                               if value.get('status') == 'failed'), None),
        }
        contests.append(summary)
        counts = Counter(code for value in holdings.values() if value.get('status') == 'success'
                         for code in value['stock_codes'])
        for row in snapshot['rows']:
            if row['contest_key'] != key:
                continue
            count = counts[row['stock_code']] if complete else None
            mismatch = count is not None and count > row['holder_count']
            note = ('千组公开持仓人数高于官方热榜人数，两个观测口径不一致；'
                    '不能相减得到其他组人数。') if mismatch else None
            if note:
                warnings.append(f'{key}/{row["market"]}/{row["stock_code"]}: {note}')
            records.append({
                'contest_key': key, 'competition_name': contest['competition_name'],
                'market': row['market'], 'stock_code': row['stock_code'],
                'stock_name': row['stock_name'], 'official_holder_count': row['holder_count'],
                'thousand_holder_count': count, 'collection_complete': complete,
                'count_exceeds_official': mismatch, 'warning': note,
            })
    complete = all(contest['collection_complete'] for contest in contests)
    metadata = {
        'schema_version': 1, 'group_code': GROUP_CODE, 'group_name': '千万组',
        **config,
        'scope': SCOPE, 'base_run_id': snapshot['manifest']['run_id'],
        'base_completed_at': snapshot['manifest']['completed_at'],
        'base_fingerprint': checkpoint['metadata']['base_fingerprint'],
        'collection_date': checkpoint['metadata']['collection_date'],
        'completed_at': now(), 'collection_complete': complete,
        'interval_seconds': interval, 'stopped_on_access_limit': stopped,
        'contest_count': len(contests), 'record_count': len(records),
        'member_count': sum(contest['member_count'] or 0 for contest in contests),
        'holdings_success_count': sum(contest['holdings_success_count'] for contest in contests),
        'holdings_failed_count': sum(contest['holdings_failed_count'] for contest in contests),
        'warnings': warnings, 'notes': NOTES,
    }
    return {'metadata': metadata, 'contests': contests, 'records': records}


def run_collection(args, client=None):
    snapshot = read_snapshot(args.snapshot)
    config = weight_config()
    client = client or PublicClient(args.interval)
    path = snapshot['path']
    checkpoint_path = path / 'thousand-group-checkpoint.json'
    checkpoint = load_checkpoint(snapshot, checkpoint_path)
    selected = [contest for contest in snapshot['contests']
                if contest['contest_key'] in snapshot['selected']]
    mappings, stopped = {}, False
    for contest in selected:
        key = contest['contest_key']
        entry = checkpoint['contests'].setdefault(key, {
            'members': [], 'holdings': {}, 'ranking_complete': False,
            'ranking_status': 'pending',
        })
        try:
            validate_member_ids(entry, contest['participant_count'])
            mappings[key] = stock_mapping([row for row in snapshot['rows'] if row['contest_key'] == key])
            entry.pop('failed_stage', None)
            entry.pop('error', None)
        except SourceError as error:
            entry.update(members=[], holdings={}, ranking_complete=False,
                         ranking_status='failed', failed_stage='validation', error=str(error))
    save(checkpoint_path, checkpoint)
    try:
        # Complete the group rosters before making individual holdings requests.
        for contest in selected:
            key = contest['contest_key']
            entry = checkpoint['contests'][key]
            if entry.get('failed_stage') or entry.get('ranking_complete') is True:
                continue
            raw_dir = path / 'competitions' / key / 'raw' / 'thousand-group'
            try:
                collect_members(client, contest, entry, raw_dir, checkpoint, checkpoint_path)
            except AccessLimited as error:
                entry.update(failed_stage='ranking', error=str(error), ranking_status='failed')
                stopped = True
                break
            except (SourceError, OSError) as error:
                entry.update(failed_stage='ranking', error=str(error), ranking_status='failed')
                save(checkpoint_path, checkpoint)
        if not stopped:
            for contest in selected:
                key = contest['contest_key']
                entry = checkpoint['contests'][key]
                if entry.get('failed_stage') or entry.get('ranking_complete') is not True:
                    continue
                raw_dir = path / 'competitions' / key / 'raw' / 'thousand-group'
                pending = [uid for uid in entry['members']
                           if entry['holdings'].get(uid, {}).get('status') != 'success']
                for index, uid in enumerate(pending, 1):
                    try:
                        entry['holdings'][uid] = collect_position(client, key, uid, mappings[key], raw_dir)
                    except AccessLimited as error:
                        entry['holdings'][uid] = {'status': 'failed', 'error': str(error), 'fetched_at': now()}
                        stopped = True
                    except (SourceError, OSError) as error:
                        entry['holdings'][uid] = {'status': 'failed', 'error': str(error), 'fetched_at': now()}
                    save(checkpoint_path, checkpoint)
                    if index % 25 == 0 or index == len(pending) or stopped:
                        print(f'Thousand-group holdings: {contest["competition_name"]}; '
                              f'{sum(v.get("status") == "success" for v in entry["holdings"].values())}'
                              f'/{len(entry["members"])} successful public lists', flush=True)
                    if stopped:
                        break
                if stopped:
                    break
    finally:
        checkpoint.setdefault('request_log', []).extend(client.events)
        save(checkpoint_path, checkpoint)
        save(path / 'thousand-group-request-log.json', checkpoint['request_log'])
        payload = build_payload(snapshot, checkpoint, args.interval, stopped, config)
        save(path / 'thousand-group.json', payload)
        print(json.dumps({'snapshot_path': str(path),
                          'thousand_group_path': str(path / 'thousand-group.json'),
                          **payload['metadata']}, ensure_ascii=False), flush=True)
    return 0 if payload['metadata']['collection_complete'] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=2.0)
    try:
        args = parser.parse_args()
        if not 2 <= args.interval < float('inf'):
            parser.error('interval must be finite and >=2 seconds')
    except SystemExit as error:
        if error.code == 2:
            return 1
        raise
    args.snapshot = args.snapshot.resolve()
    try:
        with (args.snapshot / '.thousand-group.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Another thousand-group collection is running; skipped.', flush=True)
                return 3
            return run_collection(args)
    except (OSError, ValueError, TypeError, KeyError, SourceError) as error:
        print(f'Thousand-group collection error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
