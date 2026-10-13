"""Read-only a-stock-data collector. Checkpoints one reusable input bundle; no silent sample fallback."""
import argparse
import contextlib
import hashlib
import io
import json
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from screen import dump, symbol

CN = ZoneInfo('Asia/Shanghai')


def is_main_board(sid):
    return sid.startswith(('sh600','sh601','sh603','sh605','sz000','sz001','sz002','sz003'))


def official_universe(sd, target):
    """Complete current exchange lists; failure of either exchange fails the pool."""
    out = []
    source = 'SSE official current stock list + SZSE official current A-share list; no BSE'
    as_of = datetime.now(timezone.utc).isoformat()
    for kind, board in [('1', 'MAIN'), ('8', 'STAR')]:
        rows = []
        expected = None
        page = 1
        while True:
            raw = sd._v39_http('https://query.sse.com.cn/commonQuery.do',
                params={'sqlId':'COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L','STOCK_TYPE':kind,
                        'isPagination':'true','pageHelp.pageSize':'2000','pageHelp.pageNo':str(page)},
                headers={'Referer':'https://www.sse.com.cn/assortment/stock/list/'},
                timeout=(8,20)).json()
            batch = raw.get('result')
            total = int(raw.get('pageHelp',{}).get('total',0))
            if not isinstance(batch,list) or not batch or total <= 0 or (expected is not None and total != expected):
                raise ValueError('official_sse_pool_incomplete')
            expected = total
            rows.extend(batch)
            if len(rows) >= expected:
                if len(rows) != expected:
                    raise ValueError('official_sse_pool_count_mismatch')
                break
            page += 1
            if page > 20:
                raise ValueError('official_sse_pool_pagination_limit')
        for row in rows:
            code = str(row.get('A_STOCK_CODE') or '')
            if code in ('', '-'):
                continue  # B-only companies are outside the A-share universe.
            try:
                sid = symbol('sh'+code)
            except ValueError:
                continue
            listing = datetime.strptime(row['LIST_DATE'],'%Y%m%d').date().isoformat()
            delisting = row.get('DELIST_DATE')
            if delisting not in ('', '-', None):
                removed = datetime.strptime(delisting,'%Y%m%d').date().isoformat()
                if removed <= datetime.now(CN).date().isoformat():
                    continue
            if listing > target:
                continue
            out.append({'symbol':sid,'name':row['SEC_NAME_CN'],'asset_status':'ACTIVE',
                'list_date':listing,'universe_source':source,'universe_as_of':as_of,
                'boards':{'listing_board':board,'industries':[row['CSRC_CODE_DESC']] if row.get('CSRC_CODE_DESC') else [],
                          'source':'SSE official current stock list and CSRC industry'}})
    response = sd._v39_http('https://www.szse.cn/api/report/ShowReport',
        params={'SHOWTYPE':'xlsx','CATALOGID':'1110','TABKEY':'tab1'},
        headers={'Referer':'https://www.szse.cn/market/product/stock/list/index.html'},timeout=(8,20))
    if not response.content.startswith(b'PK'):
        raise ValueError('official_szse_pool_not_workbook')
    frame = sd.pd.read_excel(io.BytesIO(response.content),dtype=str).fillna('')
    needed = {'A股代码','A股简称','A股上市日期','板块','所属行业'}
    if frame.empty or not needed.issubset(frame.columns):
        raise ValueError('official_szse_pool_incomplete')
    for row in frame.to_dict('records'):
        if not row['A股代码']:
            continue
        try:
            sid = symbol('sz'+row['A股代码'].split('.')[0].zfill(6))
        except ValueError:
            continue
        listing = sd.pd.Timestamp(row['A股上市日期']).date().isoformat()
        if listing > target:
            continue
        if row['板块'] not in ('主板','创业板'):
            raise ValueError('official_szse_unknown_listing_board')
        out.append({'symbol':sid,'name':row['A股简称'],'asset_status':'ACTIVE',
            'list_date':listing,'universe_source':source,'universe_as_of':as_of,
            'boards':{'listing_board':'CHINEXT' if row['板块']=='创业板' else 'MAIN',
                      'industries':[row['所属行业']] if row['所属行业'] else [],
                      'source':'SZSE official current A-share list and industry'}})
    if not out or len({s['symbol'] for s in out}) != len(out):
        raise ValueError('official_pool_empty_or_duplicate')
    return sorted(out,key=lambda s:s['symbol'])


def worker(action, request):
    import data_adapter as sd
    if action == 'universe':
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                with sd.bs_session():
                    data = sd._rs_to_df(sd.bs.query_stock_basic()).to_dict('records')
        except Exception:
            return official_universe(sd, request.get('target') or datetime.now(CN).date().isoformat())
        out = []
        for row in data:
            if str(row.get('status')) != '1' or str(row.get('type')) != '1':
                continue
            try:
                sid = symbol(row['code'])
            except ValueError:
                continue
            out.append({'symbol':sid, 'name':row.get('code_name',sid), 'asset_status':'ACTIVE', 'list_date':row.get('ipoDate')})
        if not out:
            return official_universe(sd, request.get('target') or datetime.now(CN).date().isoformat())
        return out
    if action == 'finance':
        from tdx_adapter import tdx_client
        client = tdx_client(check='finance')
        try:
            raw = client.finance(symbol=request['symbol'][2:]).to_dict('records')
            if len(raw) != 1 or float(raw[0].get('zongguben',0)) <= 0:
                raise ValueError('missing_total_shares')
            return {'total_shares':float(raw[0]['zongguben']), 'source':'a-stock-data mootdx finance zongguben',
                    'as_of':datetime.now(timezone.utc).isoformat()}
        finally:
            client.close()
    raise ValueError(action)


def bounded(action, request, timeout=60):
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', action, '--request', json.dumps(request)],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f'{action}_worker_failed')
    return json.loads(proc.stdout)


def calendar_range(sd, start, end):
    cursor=start.replace(day=1);days=[]
    while cursor<=end:
        rows=sd.trading_calendar(cursor.year,cursor.month).to_dict('records')
        days.extend(r['date'] for r in rows if r['is_open'])
        cursor=(cursor.replace(day=28)+timedelta(days=4)).replace(day=1)
    return sorted(set(days))


def merge_history(old, recent, target):
    merged={r['date']:r for r in old if r['date']<=target};overlap=0
    for row in recent:
        if row['date']>target:
            continue
        if row['date'] in merged:
            for key in ['open','high','low','close','volume']:
                a,b=merged[row['date']][key],row[key]
                if abs(a-b)>1e-7*max(1,abs(a)):
                    raise ValueError('qfq_overlap_mismatch:'+row['date']+':'+key)
            overlap+=1
        merged[row['date']]=row
    if old and recent and any(r['date']<=target for r in recent) and overlap<min(20,len(old),sum(r['date']<=target for r in recent)):
        raise ValueError('qfq_overlap_insufficient')
    return [merged[d] for d in sorted(merged)]


def batch_quotes(sd, ids, cache_path):
    cache_path = Path(cache_path)
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    quotes = {}
    for offset in range(0,len(ids),100):
        raw = sd._v39_http('https://qt.gtimg.cn/q='+','.join(ids[offset:offset+100]),timeout=(8,20)).content.decode('gbk')
        for sid,body in re.findall(r'v_((?:sh|sz)\d{6})="([^"]*)"',raw):
            quotes[sid] = body.split('~')
    dump(cache_path,quotes)
    return quotes


def reusable_bundle(path, target, required_lookback_days=1600):
    """Validate an explicit same-day snapshot without importing network dependencies."""
    source = Path(path)
    bundle = json.loads(source.read_text())
    if bundle.get('schema_version') != '1.0' or bundle.get('target_date') != target:
        raise ValueError('reuse_input_target_or_schema_mismatch')
    if bundle.get('scope') not in ('subset', 'current_active_sh_sz'):
        raise ValueError('reuse_input_scope_unsupported')
    if target not in bundle.get('market_days', []) or not bundle.get('calendar_source') or not bundle.get('universe_source'):
        raise ValueError('reuse_input_missing_calendar_or_universe')
    request = bundle.get('collection_request')
    required_start = date.fromisoformat(target) - timedelta(days=required_lookback_days)
    if (not isinstance(request, dict) or request.get('adjust') != 'qfq'
            or date.fromisoformat(request.get('start_date', '9999-12-31')) > required_start):
        raise ValueError('reuse_input_history_window_too_short_or_unknown')
    stocks = bundle.get('stocks')
    if not isinstance(stocks, list) or not stocks:
        raise ValueError('reuse_input_empty_universe')
    seen = set()
    cutoff = datetime.combine(date.fromisoformat(target), time(15), CN)
    for stock in stocks:
        sid = symbol(stock.get('symbol'))
        if sid in seen:
            raise ValueError('reuse_input_duplicate_stock:' + sid)
        seen.add(sid)
        if stock.get('errors'):
            continue
        if stock.get('adjust') != 'qfq' or not stock.get('bar_source'):
            raise ValueError('reuse_input_not_qfq:' + sid)
        stamp = datetime.fromisoformat(stock['bars_as_of'].replace('Z', '+00:00'))
        bars = stock.get('bars')
        if stamp.tzinfo is None or stamp.astimezone(CN) < cutoff or not isinstance(bars, list) or not bars or bars[-1].get('date') != target:
            raise ValueError('reuse_input_not_confirmed_close:' + sid)
    return bundle, hashlib.sha256(source.read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',default='latest')
    p.add_argument('--symbols',help='Explicit comma-separated stocks; scope will be subset')
    p.add_argument('--universe',type=Path,help='JSON array of current ACTIVE symbols/names')
    p.add_argument('--reuse-input',type=Path,help='Reuse a verified same-date normalized bundle without market requests')
    p.add_argument('--out',type=Path)
    p.add_argument('--main-board-only',action='store_true')
    p.add_argument('--lookback-days',type=int,default=1600,help='Calendar days; default supports 741 daily / 114 weekly bars')
    p.add_argument('--worker',choices=['universe','finance'],help=argparse.SUPPRESS)
    p.add_argument('--request',default='{}',help=argparse.SUPPRESS)
    args=p.parse_args()
    if args.worker:
        # Third-party libraries may print connection messages; only JSON reaches stdout.
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture):
            result=worker(args.worker,json.loads(args.request))
        print(json.dumps(result,ensure_ascii=False));return
    if not args.out:
        p.error('--out is required')
    if args.out.exists():
        p.error('Input bundle already exists; use a new output path')
    if sum(bool(x) for x in (args.symbols,args.universe,args.reuse_input)) > 1:
        p.error('Choose only one of --symbols, --universe or --reuse-input')
    if args.reuse_input:
        if args.date == 'latest':
            p.error('--reuse-input requires an explicit YYYY-MM-DD date to prevent stale reuse')
        try:
            target = date.fromisoformat(args.date).isoformat()
            bundle, digest = reusable_bundle(args.reuse_input, target, args.lookback_days)
            if args.main_board_only:
                bundle['stocks']=[s for s in bundle['stocks'] if is_main_board(symbol(s['symbol']))]
                if not bundle['stocks']:
                    raise ValueError('empty_main_board_universe')
                bundle.update(scope='subset',universe_filter='MAIN')
                bundle['universe_source']+='; user-requested SH/SZ MAIN filter'
            dump(args.out, bundle)
            print(json.dumps({'out':str(args.out.resolve()),'target_date':target,'stocks':len(bundle['stocks']),
                              'reused_input_sha256':digest,'network_requests':0},ensure_ascii=False))
            return
        except Exception as exc:
            dump(args.out.with_suffix('.failure.json'),{'status':'failed','error':type(exc).__name__+':'+str(exc)})
            raise SystemExit('Reuse failed: '+str(exc))
    try:
        import data_adapter as sd
        sd.EM_MIN_INTERVAL=1.5
        now=datetime.now(CN)
        requested=now.date() if args.date=='latest' else date.fromisoformat(args.date)
        if requested>now.date():
            raise ValueError('target_in_future')
        start=requested-timedelta(days=args.lookback_days)
        market_days=calendar_range(sd,start,requested)
        closed=[d for d in market_days if d<=requested.isoformat() and datetime.combine(date.fromisoformat(d),time(15),CN)<=now]
        if not closed:
            raise ValueError('no_confirmed_closed_market_day')
        target=max(closed) if args.date=='latest' else requested.isoformat()
        if target not in closed:
            raise ValueError('target_not_closed_trading_day')
        scope='subset' if args.symbols else 'current_active_sh_sz'
        if args.symbols:
            universe=[{'symbol':symbol(v),'asset_status':'ACTIVE'} for v in args.symbols.split(',')]
            universe_source='user-specified subset; verify current listing through live quote'
        elif args.universe:
            universe=json.loads(args.universe.read_text())
            universe_source='user-supplied current ACTIVE universe: '+args.universe.name
        else:
            universe=bounded('universe',{'target':target},90)
            universe_source='a-stock-data baostock stock_basic: status=1,type=1; current active SH/SZ A-share prefixes'
            pool_sources={s.get('universe_source',universe_source) for s in universe}
            if len(pool_sources)!=1:
                raise ValueError('mixed_universe_sources')
            universe_source=next(iter(pool_sources))
        if args.main_board_only:
            universe=[s for s in universe if is_main_board(symbol(s['symbol']))]
            scope='subset'
            universe_source+='; user-requested SH/SZ MAIN filter'
        if not universe:
            raise ValueError('empty_universe')
        ids=[symbol(r['symbol']) for r in universe]
        if len(ids)!=len(set(ids)):
            raise ValueError('duplicate_universe_members')
        bundle={'schema_version':'1.0','target_date':target,'scope':scope,'universe_source':universe_source,
                'collection_request':{'start_date':start.isoformat(),'end_date':target,'lookback_days':args.lookback_days,'adjust':'qfq'},
                'market_days':market_days,'calendar_source':'a-stock-data SZSE official month calendar',
                'sources':{'bars':'a-stock-data Tencent qfq','finance':'Eastmoney stock info / mootdx finance',
                           'boards':'Eastmoney slist / listing code classification'},'stocks':[]}
        if args.main_board_only:
            bundle['universe_filter']='MAIN'
        finance_failures=0
        checkpoint_dir=args.out.parent/(args.out.stem+'.stock-cache')
        checkpoint_dir.mkdir(parents=True,exist_ok=True)
        quotes=batch_quotes(sd,ids,args.out.with_suffix('.quotes.json'))
        for member,sid in zip(universe,ids):
            checkpoint=checkpoint_dir/(sid+'.json')
            if checkpoint.exists():
                cached=json.loads(checkpoint.read_text())
                if not cached.get('errors') and cached.get('bars') and cached['bars'][-1]['date']==target:
                    bundle['stocks'].append(cached)
                    continue
            item={**member,'symbol':sid,'adjust':'qfq','bar_source':'Tencent fqkline qfq via a-stock-data',
                  'bars_as_of':datetime.now(timezone.utc).isoformat(),'bars':[],'errors':[]}
            # Listing market is explicit metadata; mixed sector tags do not define CHINEXT/STAR.
            listing='STAR' if sid.startswith(('sh688','sh689')) else 'CHINEXT' if sid.startswith(('sz300','sz301')) else 'MAIN'
            item['boards']={'listing_board':listing,'source':'SH/SZ listing code classification; Eastmoney sector labels when available'}
            try:
                item['bars']=sd.tencent_kline(sid,start=start.isoformat(),end=target,adjust='qfq').to_dict('records')
                fields=quotes.get(sid,[])
                stamp=fields[30] if len(fields)>30 else ''
                if len(fields)<53 or len(stamp)!=14:
                    raise ValueError('live_quote_or_listing_unverified')
                price=float(fields[3] or 0);last_close=float(fields[4] or 0);amount_wan=float(fields[37] or 0)
                if amount_wan == 0 and price == last_close and price > 0:
                    raise ValueError('live_quote_or_listing_unverified')
                item['name']=fields[1]
                quote_date=datetime.strptime(stamp,'%Y%m%d%H%M%S').date().isoformat()
                latest=next((r for r in reversed(item['bars']) if r['date']==quote_date),None)
                if not latest or stamp[8:]<'150000':
                    raise ValueError('after_close_volume_unit_check_unavailable')
                low=float(fields[34] or 0);high=float(fields[33] or 0)
                ratio=amount_wan*10000/latest['volume']
                choices=[div for div in [1,100] if low<=ratio*div/100<=high]
                if len(choices)!=1:
                    raise ValueError('volume_unit_ambiguous')
                item['volume_unit']='shares' if choices[0]==100 else 'lots'
                item['volume_unit_source']='inferred: unique unit consistent with same-day amount and unadjusted price range on '+quote_date
                item['quote']={'date':quote_date,'amount_yi':amount_wan/10000,
                               'turnover_pct':float(fields[38]) if fields[38] else None}
                item['bars_as_of']=datetime.now(timezone.utc).isoformat()
            except Exception as exc:
                item['errors'].append(type(exc).__name__+':'+str(exc))
            try:
                sectors=sd.eastmoney_concept_blocks(sid[2:])
                item['boards'].update(sectors,quote_date=now.date().isoformat())
            except Exception as exc:
                item['board_error']=type(exc).__name__+':'+str(exc)
            try:
                info=sd.eastmoney_stock_info(sid[2:])
                if info.get('industry'):
                    item['boards']['industries']=[info['industry']]
                if not float(info.get('total_shares') or 0)>0:
                    raise ValueError('missing_total_shares')
                item['finance']={'total_shares':float(info['total_shares']),'source':'Eastmoney stock/get total_shares','as_of':now.isoformat()}
            except Exception:
                if finance_failures<3:
                    try:
                        item['finance']=bounded('finance',{'symbol':sid},60)
                    except Exception as exc:
                        finance_failures+=1;item['finance_error']=type(exc).__name__+':'+str(exc)
                else:
                    item['finance_error']='finance fallback circuit open after 3 failures; missing shares remain unevaluated'
            bundle['stocks'].append(item)
            # Persist only this stock, not the growing whole-market history on every iteration.
            dump(checkpoint,item)
            print(json.dumps({'symbol':sid,'completed':len(bundle['stocks']),'pool':len(universe),'errors':item['errors']},ensure_ascii=False),flush=True)
        dump(args.out,bundle)
    except Exception as exc:
        dump(args.out.with_suffix('.failure.json'),{'status':'failed','error':type(exc).__name__+':'+str(exc)})
        raise SystemExit('Collection failed: '+str(exc))


if __name__=='__main__':
    main()
