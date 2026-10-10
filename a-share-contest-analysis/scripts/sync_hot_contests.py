"""Collect contest snapshots, analyze daily changes, and publish a local report."""
import argparse
import fcntl
import html
import json
import signal
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
TZ = ZoneInfo('Asia/Shanghai')
sys.path.insert(0, str(SCRIPTS))
from trading_calendar import CalendarUnavailable, trading_day_status
NETWORK_SCRIPTS = {'collect_hot_contests.py', 'collect_thousand_group.py', 'collect_capital_groups.py'}


class CollectionPaused(Exception):
    pass


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def command(script, arguments):
    invocation = [sys.executable, str(SCRIPTS / script), *arguments]
    pause_error = None
    if script not in NETWORK_SCRIPTS:
        result = subprocess.run(invocation, capture_output=True, text=True)
    else:
        def check_day():
            is_trading_day, reason = trading_day_status(datetime.now(TZ).date())
            if not is_trading_day:
                raise CollectionPaused(reason)
        check_day()
        process = subprocess.Popen(invocation, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while True:
            try:
                moment = datetime.now(TZ)
                midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                timeout = max(.001, min(30, (midnight - moment).total_seconds()))
                stdout, stderr = process.communicate(timeout=timeout)
                break
            except subprocess.TimeoutExpired:
                try:
                    check_day()
                except (CollectionPaused, CalendarUnavailable) as error:
                    pause_error = error
                    process.send_signal(signal.SIGINT)
                    stdout, stderr = process.communicate()
                    break
        result = subprocess.CompletedProcess(invocation, process.returncode, stdout, stderr)
    if result.stdout:
        print(result.stdout, end='', flush=True)
    if result.stderr:
        print(result.stderr, end='', file=sys.stderr, flush=True)
    if script in NETWORK_SCRIPTS and pause_error is not None:
        raise pause_error
    return result


def last_json(result):
    lines = result.stdout.strip().splitlines()
    if not lines:
        raise ValueError('Workflow command returned no result')
    value = json.loads(lines[-1])
    if not isinstance(value, dict):
        raise ValueError('Workflow command returned an invalid result')
    return value


def sync(args):
    hot_only = getattr(args, 'hot_only', False)
    stamp = datetime.now(TZ).strftime('%Y-%m-%d_%H%M%S_%f')
    status = {'started_at': now(), 'analyze_only': args.analyze_only,
              'collection_mode': 'official_hot_lists' if hot_only else 'capital_groups',
              'stage': 'starting', 'complete': False, 'exit_code': None}
    history = args.report_root / 'sync-history' / (stamp + '.json')
    try:
        if not args.analyze_only:
            status['stage'] = 'trading_day_check'
            trading_date = datetime.now(TZ).date()
            status['trading_date'] = trading_date.isoformat()
            try:
                is_trading_day, reason = trading_day_status(trading_date)
            except CalendarUnavailable as error:
                status.update(exit_code=2, error=str(error))
                return 2
            if not is_trading_day:
                status.update(stage='skipped', skipped=True, skip_reason=reason, exit_code=0)
                return 0
        if args.analyze_only:
            status['stage'] = 'select_snapshot'
            if args.snapshot:
                snapshot = args.snapshot.resolve()
            else:
                pointer = json.loads((args.snapshots / 'latest.json').read_text(encoding='utf-8'))
                snapshot = (args.snapshots / pointer['relative_path']).resolve()
        else:
            status['stage'] = 'collection'
            result = command('collect_hot_contests.py', [
                '--threshold', str(args.threshold), '--interval', str(args.interval),
                '--output', str(args.snapshots), '--registry', str(args.registry),
            ])
            if result.returncode:
                status['exit_code'] = result.returncode
                status['error'] = 'Collection did not complete; existing published report retained.'
                return result.returncode
            snapshot = Path(last_json(result)['run_directory']).resolve()
        status['snapshot_path'] = str(snapshot)
        if not args.analyze_only and not hot_only:
            status['stage'] = 'thousand_group_collection'
            result = command('collect_thousand_group.py', [
                '--snapshot', str(snapshot), '--interval', str(args.interval),
            ])
            if result.returncode:
                status.update(exit_code=result.returncode,
                              error='Thousand-group collection did not complete; published report retained.')
                return result.returncode
            status['stage'] = 'capital_group_collection'
            result = command('collect_capital_groups.py', [
                '--snapshot', str(snapshot), '--interval', str(args.interval),
            ])
            if result.returncode:
                status.update(exit_code=result.returncode,
                              error='Capital-group collection did not complete; published report retained.')
                return result.returncode
        status['stage'] = 'analysis'
        result = command('analyze_hot_contests.py', [
            '--snapshot', str(snapshot), '--snapshots', str(args.snapshots),
            '--report-root', str(args.report_root), '--no-publish',
        ] + (['--hot-only'] if hot_only else []))
        if result.returncode:
            status.update(exit_code=result.returncode, error='Analysis failed; published report retained.')
            return result.returncode
        report_dir = Path(last_json(result)['report_directory']).resolve()
        report = json.loads((report_dir / 'analysis.json').read_text(encoding='utf-8'))
        status.update(report_directory=str(report_dir), review_status=report['summary']['review_status'])
        if not args.analyze_only and not hot_only and report['summary'].get('group_weighting_available') is not True:
            status.update(exit_code=2,
                          error='Four capital groups failed validation; published report retained.',
                          group_validation_warnings=report['metadata']['group_collection'].get('warnings', []))
            return 2
        status['stage'] = 'render'
        result = command('render_hot_contest_report.py', [str(report_dir / 'analysis.json')])
        if result.returncode or not (report_dir / 'index.html').is_file():
            status.update(exit_code=result.returncode or 1, error='HTML rendering failed; published report retained.')
            return status['exit_code']
        status['stage'] = 'publish'
        relative = report_dir.relative_to(args.report_root).as_posix()
        pointer = {'relative_path': relative, 'report_date': report['metadata']['report_date'],
                   'run_id': report['metadata']['run_id'], 'generated_at': report['metadata']['generated_at'],
                   'html_path': relative + '/index.html'}
        link = html.escape(relative + '/index.html', quote=True)
        redirect = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    f'<meta http-equiv="refresh" content="0;url={link}">'
                    f'<title>热门比赛每日复盘</title><a href="{link}">打开最新复盘报告</a></html>')
        temporary = args.report_root / 'index.html.tmp'
        temporary.write_text(redirect, encoding='utf-8')
        index_path = args.report_root / 'index.html'
        previous_index = index_path.read_bytes() if index_path.exists() else None
        temporary.replace(index_path)
        try:
            save(args.report_root / 'latest.json', pointer)
        except OSError:
            # Keep both published entry points on the previous complete report.
            if previous_index is None:
                index_path.unlink(missing_ok=True)
            else:
                temporary.write_bytes(previous_index)
                temporary.replace(index_path)
            raise
        status.update(stage='complete', complete=True, exit_code=0,
                      report_date=report['metadata']['report_date'],
                      html_path=str(report_dir / 'index.html'), summary=report['summary'])
        return 0
    except CollectionPaused as error:
        status.update(stage='skipped', skipped=True, skip_reason=f'采集中进入休市日，检查点已保留：{error}', exit_code=0)
        return 0
    except CalendarUnavailable as error:
        status.update(exit_code=2, error=str(error))
        return 2
    except (OSError, ValueError, KeyError) as error:
        status.update(exit_code=1, error=str(error))
        print(f'Synchronization error: {error}', file=sys.stderr)
        return 1
    finally:
        status['completed_at'] = now()
        save(history, status)
        save(args.report_root / 'sync-latest.json', status)
        print(json.dumps(status, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analyze-only', action='store_true', help='Analyze a saved complete snapshot without network requests')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--hot-only', dest='hot_only', action='store_true', help='Collect official hot lists only (default)')
    mode.add_argument('--capital-groups', dest='hot_only', action='store_false', help='Explicitly collect all four member/holding tiers before weighting')
    parser.set_defaults(hot_only=True)
    parser.add_argument('--snapshot', type=Path, help='Explicit snapshot, only with --analyze-only')
    parser.add_argument('--snapshots', type=Path, default=ROOT / 'exports' / 'hot-contests')
    parser.add_argument('--report-root', type=Path, default=ROOT / 'exports' / 'hot-contest-analysis')
    parser.add_argument('--registry', type=Path, default=ROOT / 'data' / 'contest-registry.json')
    parser.add_argument('--threshold', type=int, default=1000)
    parser.add_argument('--interval', type=float, default=2.0)
    try:
        args = parser.parse_args()
        if args.snapshot and not args.analyze_only:
            parser.error('--snapshot requires --analyze-only')
        if args.threshold < 0 or args.interval < 2 or not args.interval < float('inf'):
            parser.error('threshold must be >=0; interval must be finite and >=2')
    except SystemExit as error:
        if error.code == 2:
            return 1
        raise
    args.snapshots = args.snapshots.resolve()
    args.report_root = args.report_root.resolve()
    args.registry = args.registry.resolve()
    args.report_root.mkdir(parents=True, exist_ok=True)
    with (args.report_root / '.daily-sync.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another daily synchronization is running; skipped.', flush=True)
            return 3
        return sync(args)


if __name__ == '__main__':
    sys.exit(main())
