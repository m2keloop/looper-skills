"""Snapshot public contest hot-stock rankings, without accounts or credentials."""
import argparse
import csv
import fcntl
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo('Asia/Shanghai')
DIRECTORY_URL = 'https://buhui.ai/api/v1/competitions'
BASE = 'https://capital.hexin.cn/caishen_httpserver/direct/caishen_fund/community_share/v1/'
SHARE = 'https://tzzb.10jqka.com.cn/tzzbWeb/community/'
SCOPE = 'buhui公开比赛目录及本地registry中的比赛；不是同花顺平台全部比赛目录。'
STOCK_SCOPE = '官方热股榜本次返回的热门股票；不是全部股票或逐人持仓明细。'


class SourceError(Exception):
    pass


class AccessLimited(SourceError):
    pass


class RequestsStopped(SourceError):
    pass


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def timestamp(value):
    if value in (None, '', 0, '0'):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, TZ).isoformat(timespec='seconds')
    except (ValueError, TypeError, OverflowError, OSError) as error:
        raise SourceError('Invalid source timestamp') from error


class PublicClient:
    def __init__(self, interval, stop_event=None):
        self.interval = interval
        self.last_started = 0
        self.events = []
        self.stop_event = stop_event

    def request(self, url):
        for attempt in range(1, 4):
            delay = max(0, self.last_started + self.interval - time.monotonic())
            if self.stop_event is None:
                time.sleep(delay)
            elif self.stop_event.wait(delay):
                raise RequestsStopped('Concurrent collection stopped; request not started')
            self.last_started = time.monotonic()
            result = subprocess.run([
                'curl', '-sS', '--connect-timeout', '10', '--max-time', '25',
                '--proto', '=https', '--proto-redir', '=https', '--location',
                '--max-redirs', '3', '--write-out', '\n%{http_code}', url,
            ], capture_output=True, text=True)
            body, _, status = result.stdout.rpartition('\n')
            parts = urlsplit(url)
            safe_query = [(key, 'sha256:' + hashlib.sha256(value.encode('utf-8')).hexdigest()
                           if key == 'user_key' else value)
                          for key, value in parse_qsl(parts.query, keep_blank_values=True)]
            safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                   urlencode(safe_query), parts.fragment))
            event = {'url': safe_url, 'requested_at': now(), 'attempt': attempt,
                     'http_status': status, 'curl_exit_code': result.returncode}
            self.events.append(event)
            if status in ('403', '429'):
                if self.stop_event is not None:
                    self.stop_event.set()
                raise AccessLimited(f'HTTP {status}: stopped; no further source requests')
            if result.returncode == 0 and status == '200':
                try:
                    return json.loads(body)
                except ValueError as error:
                    raise SourceError('HTTP 200 response is not JSON') from error
            if result.returncode != 0 or status == '408' or status.startswith('5'):
                if attempt < 3:
                    if self.stop_event is None:
                        time.sleep(attempt * 2)
                    elif self.stop_event.wait(attempt * 2):
                        raise RequestsStopped('Concurrent collection stopped; retry not started')
                    continue
            raise SourceError(f'Request failed: HTTP {status or "unknown"}, curl exit {result.returncode}')


def source_data(payload):
    if not isinstance(payload, dict) or str(payload.get('error_code')) != '0':
        raise SourceError('Official source returned an unsuccessful response')
    if not isinstance(payload.get('ex_data'), dict):
        raise SourceError('Official source has no data object')
    return payload['ex_data']


def seed_items(items):
    if not isinstance(items, list):
        raise SourceError('Contest directory has no items list')
    seeds = {}
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get('key'), str)
                or not re.fullmatch(r'[A-Za-z0-9]{4,32}', item['key'])):
            raise SourceError('Contest directory contains an invalid share key')
        key = item['key']
        seeds[key] = {'key': key, 'title': str(item.get('title') or key)}
    return seeds


def official_url(endpoint, key, **extra):
    return BASE + endpoint + '?' + urlencode({'terminal': 1, 'version': 2, 'key': key, **extra})


def hot_stocks(data, info, fetched_at):
    rows = data.get('hot_stock_list')
    if not isinstance(rows, list) or data.get('match_name') != info['competition_name']:
        raise SourceError('Hot-stock response is missing rows or belongs to another contest')
    stocks = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise SourceError('Hot-stock row is not an object')
        code, name, market = row.get('stock_code'), row.get('stock_name'), row.get('market')
        if not isinstance(code, str) or not code or not isinstance(name, str) or not name:
            raise SourceError('Hot-stock row has no stock identity')
        if not isinstance(market, (str, int)) or str(market) == '':
            raise SourceError('Hot-stock row has no valid market identity')
        market = str(market)
        count = str(row.get('hold_num', ''))
        if not re.fullmatch(r'\d+', count) or (market, code) in seen:
            raise SourceError('Hot-stock row has an invalid count or duplicate identity')
        seen.add((market, code))
        stocks.append({'stock_code': code, 'stock_name': name,
                       'market': str(market or ''), 'holder_count': int(count)})
    stocks.sort(key=lambda stock: -stock['holder_count'])
    for rank, stock in enumerate(stocks, 1):
        stock['stock_rank'] = rank
    metadata = {**info, 'source_updated_at': timestamp(data.get('time')),
                'fetched_at': fetched_at, 'returned_stock_count': len(stocks),
                'source_url': SHARE + 'hotStockPageWeb.html?key=' + info['contest_key'],
                'stock_scope': STOCK_SCOPE,
                'counting_note': 'holder_count为官方hold_num（页面持仓人数）；统计覆盖范围未独立验证。'}
    return {'metadata': metadata, 'stocks': stocks}


def write_csv(path, fields, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def run_collection(args, client=None):
    client = client or PublicClient(args.interval)
    registry = json.loads(args.registry.read_text(encoding='utf-8'))
    seeds = seed_items(registry['items'])
    run_id = datetime.now(TZ).strftime('%Y-%m-%d_%H%M%S_%f')
    run_dir = args.output / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    started = now()
    directory_ok, stopped, warnings = False, False, []
    try:
        directory = client.request(DIRECTORY_URL)
        save(run_dir / 'raw' / 'directory.json', directory)
        if not isinstance(directory, dict) or directory.get('code') != 0:
            raise SourceError('Public directory returned an unsuccessful response')
        if not isinstance(directory.get('data'), dict):
            raise SourceError('Public directory has no data object')
        found = seed_items(directory['data'].get('items'))
        if not found:
            raise SourceError('Public directory is empty')
        seeds.update(found)
        save(args.registry, {'scope': SCOPE, 'directory_url': DIRECTORY_URL,
                             'updated_at': now(), 'items': list(seeds.values())})
        directory_ok = True
    except AccessLimited as error:
        stopped = True
        warnings.append(str(error))
    except SourceError as error:
        warnings.append(f'Directory unavailable; using saved registry: {error}')

    catalog = [{'contest_key': key, 'competition_name': seed['title'],
                'participant_count': None, 'participant_count_checked_at': None,
                'contest_status': None, 'qualifies': None, 'collection_status': 'pending'}
               for key, seed in seeds.items()]
    records = []
    for entry in catalog:
        if stopped:
            break
        key = entry['contest_key']
        contest_dir = run_dir / 'competitions' / key
        stage = 'participant_count'
        try:
            ranking = client.request(official_url('match_ranking_by_share', key, month='',
                                                sort_type='', sort_order=0, group_type='', page=1))
            save(contest_dir / 'raw' / 'ranking-page-1.json', ranking)
            data = source_data(ranking)
            count = str(data.get('max_count', ''))
            if not re.fullmatch(r'\d+', count) or not data.get('article_title'):
                raise SourceError('Ranking has no valid participant count or title')
            entry.update(competition_name=data['article_title'], participant_count=int(count),
                         participant_count_checked_at=now(), contest_status=data.get('status'),
                         match_start=data.get('match_start'), match_end=data.get('match_end'),
                         qualifies=int(count) > args.threshold)
            if not entry['qualifies']:
                entry['collection_status'] = 'excluded'
                print(f'Excluded: {entry["competition_name"]} ({count} <= {args.threshold})', flush=True)
                continue
            stage = 'hot_stocks'
            payload = client.request(official_url('match_stock_ranking_by_share', key))
            save(contest_dir / 'raw' / 'hot-stock-response.json', payload)
            info = {field: entry[field] for field in ('contest_key', 'competition_name',
                    'participant_count', 'participant_count_checked_at', 'contest_status')}
            result = hot_stocks(source_data(payload), info, now())
            save(contest_dir / 'hot-stocks.json', result)
            metadata = result['metadata']
            for stock in result['stocks']:
                records.append({**info, 'source_updated_at': metadata['source_updated_at'],
                                'fetched_at': metadata['fetched_at'], **stock})
            entry.update(collection_status='collected', returned_stock_count=len(result['stocks']),
                         source_updated_at=metadata['source_updated_at'])
            print(f'Collected: {entry["competition_name"]}; {count} participants; '
                  f'{len(result["stocks"])} hot stocks; source {metadata["source_updated_at"]}', flush=True)
        except AccessLimited as error:
            entry.update(collection_status='failed', error=str(error), failed_stage=stage)
            stopped = True
            warnings.append(str(error))
        except SourceError as error:
            entry.update(collection_status='failed', error=str(error), failed_stage=stage)
        finally:
            save(run_dir / 'catalog.json', {'scope': SCOPE, 'threshold': args.threshold, 'items': catalog})
            save(run_dir / 'request-log.json', client.events)

    catalog.sort(key=lambda item: -(item['participant_count'] if item['participant_count'] is not None else -1))
    records.sort(key=lambda item: (-item['participant_count'], item['contest_key'], item['stock_rank']))
    complete = directory_ok and all(item['collection_status'] in ('collected', 'excluded') for item in catalog)
    manifest = {'schema_version': 1, 'run_id': run_id, 'started_at': started, 'completed_at': now(),
                'scope': SCOPE, 'stock_scope': STOCK_SCOPE,
                'threshold': args.threshold, 'selection_rule': 'participant_count > threshold',
                'interval_seconds': args.interval, 'directory_refreshed': directory_ok,
                'catalog_entry_count': len(catalog),
                'participant_counts_checked': sum(item['participant_count'] is not None for item in catalog),
                'hot_contest_count': sum(item['qualifies'] is True for item in catalog),
                'collected_contest_count': sum(item['collection_status'] == 'collected' for item in catalog),
                'failed_contest_count': sum(item['collection_status'] == 'failed' for item in catalog),
                'pending_contest_count': sum(item['collection_status'] == 'pending' for item in catalog),
                'stock_row_count': len(records),
                'unique_returned_stock_count': len({(row['market'], row['stock_code']) for row in records}),
                'collection_complete': complete, 'stopped_on_access_limit': stopped, 'warnings': warnings,
                'cross_contest_note': '同一人可能参加多场比赛，不能把各比赛持仓人数相加当作独立投资者人数。'}
    save(run_dir / 'manifest.json', manifest)
    save(run_dir / 'catalog.json', {'scope': SCOPE, 'threshold': args.threshold, 'items': catalog})
    save(run_dir / 'request-log.json', client.events)
    save(run_dir / 'all-hot-stocks.json', {'metadata': manifest, 'records': records})
    write_csv(run_dir / 'all-hot-stocks.csv', ['contest_key', 'competition_name', 'participant_count',
              'participant_count_checked_at', 'contest_status', 'source_updated_at', 'fetched_at',
              'stock_rank', 'stock_code', 'stock_name', 'market', 'holder_count'], records)
    write_csv(run_dir / 'contests.csv', ['contest_key', 'competition_name', 'participant_count',
              'participant_count_checked_at', 'contest_status', 'match_start', 'match_end',
              'qualifies', 'collection_status', 'returned_stock_count', 'source_updated_at',
              'failed_stage', 'error'], catalog)
    if complete:
        save(args.output / 'latest.json', {'run_id': run_id, 'relative_path': run_id,
                                         'completed_at': manifest['completed_at']})
    print(json.dumps({'run_directory': str(run_dir), **manifest}, ensure_ascii=False), flush=True)
    return 0 if complete else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--threshold', type=int, default=1000, help='Select participant count strictly greater than this')
    parser.add_argument('--interval', type=float, default=2.0, help='Minimum seconds between request starts')
    parser.add_argument('--output', type=Path, default=ROOT / 'exports' / 'hot-contests')
    parser.add_argument('--registry', type=Path, default=ROOT / 'data' / 'contest-registry.json')
    try:
        args = parser.parse_args()
        if args.threshold < 0 or args.interval < 1 or not args.interval < float('inf'):
            parser.error('threshold must be >=0; interval must be finite and >=1 second')
    except SystemExit as error:
        if error.code == 2:
            return 1
        raise
    args.output = args.output.resolve()
    args.registry = args.registry.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with (args.output / '.collection.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Another collection is running; skipped.', flush=True)
                return 3
            return run_collection(args)
    except (OSError, ValueError, KeyError, SourceError) as error:
        print(f'Collection error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
