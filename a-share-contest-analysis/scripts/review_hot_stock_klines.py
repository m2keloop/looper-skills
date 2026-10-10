"""Compare official hot-list equities with recent public daily candles."""
import argparse
import json
import math
import statistics
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
SOURCE = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
SOURCE_NAME = 'Tencent public daily candle feed'
ADJUSTMENT = 'qfq for chart/period returns; unadjusted for daily limit-like proxies'


def iso_date(value, label):
    if not isinstance(value, str):
        raise ValueError(f'{label} must be an ISO date')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be an ISO date') from exc
    if parsed.isoformat() != value:
        raise ValueError(f'{label} must be an ISO date')
    return value


def validate_series(rows, label):
    if not isinstance(rows, list) or not rows:
        raise ValueError(f'Missing daily candle series: {label}')
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f'Invalid daily candle: {label}')
        day = iso_date(row.get('date'), f'{label} candle date')
        values = [row.get(key) for key in ('open', 'close', 'high', 'low', 'volume')]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
            raise ValueError(f'Invalid daily candle numbers: {label}')
        o, c, h, l, v = [float(value) for value in values]
        if not all(math.isfinite(value) for value in (o, c, h, l, v)):
            raise ValueError(f'Non-finite daily candle: {label}')
        if not (0 < l <= min(o, c) <= max(o, c) <= h and v >= 0):
            raise ValueError(f'Daily candle violates OHLC bounds: {label}')
        normalized.append(dict(date=day, open=o, close=c, high=h, low=l, volume=v))
    dates = [row['date'] for row in normalized]
    if dates != sorted(set(dates)):
        raise ValueError(f'Daily candle dates are not unique and ascending: {label}')
    return normalized


def load_candles(path):
    """Read a previous candles.json, without trusting cached derived metrics."""
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or not isinstance(payload.get('metadata'), dict):
        raise ValueError('Unsupported candles format: expected metadata and stocks')
    metadata = payload['metadata']
    if metadata.get('source') != SOURCE_NAME or metadata.get('adjustment') != ADJUSTMENT:
        raise ValueError('Unsupported candle source or adjustment declaration')
    latest = iso_date(metadata.get('latest_market_date'), 'Cached latest market date')
    fetched_at = metadata.get('fetched_at')
    if not isinstance(fetched_at, str):
        raise ValueError('Cached candles must declare their original fetched_at')
    try:
        fetched = datetime.fromisoformat(fetched_at.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Invalid cached fetched_at') from exc
    if fetched.tzinfo is None or latest > fetched.astimezone(TZ).date().isoformat():
        raise ValueError('Cached fetched_at must include a timezone and follow the market date')
    rows = payload.get('stocks')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Cached candles do not contain stocks')
    stocks = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid cached stock entry')
        code = row.get('stock_code')
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit() or code in stocks:
            raise ValueError('Cached stock codes must be unique six-digit strings')
        symbol = ('sh' if code.startswith('6') else 'sz') + code
        if row.get('symbol') != symbol:
            raise ValueError(f'Cached candle symbol does not match {code}')
        sources = row.get('sources')
        if not isinstance(sources, dict):
            raise ValueError(f'Missing cached source URLs for {code}')
        for field, adjustment in (('qfq', 'qfq'), ('unadjusted', '')):
            url = sources.get(field)
            if not isinstance(url, str):
                raise ValueError(f'Missing cached {field} source URL for {code}')
            parsed = urlparse(url)
            if (f'{parsed.scheme}://{parsed.netloc}{parsed.path}' != SOURCE
                    or parse_qs(parsed.query).get('param') != [f'{symbol},day,,,120,{adjustment}']):
                raise ValueError(f'Unsupported cached {field} source URL for {code}')
        qfq = validate_series(row.get('qfq'), f'{code} qfq')
        raw = validate_series(row.get('unadjusted'), f'{code} unadjusted')
        if [day['date'] for day in qfq] != [day['date'] for day in raw]:
            raise ValueError(f'Cached adjusted and unadjusted trading dates differ for {code}')
        if qfq[-1]['date'] != latest or raw[-1]['date'] != latest:
            raise ValueError(f'Cached candle dates do not match metadata for {code}')
        stocks[code] = {'qfq': qfq, 'unadjusted': raw, 'sources': sources}
    return metadata, stocks


def request(symbol, adjustment, last_started):
    time.sleep(max(0, last_started + 2 - time.monotonic()))
    started = time.monotonic()
    url = SOURCE + '?' + urlencode({'param': f'{symbol},day,,,120,{adjustment}'})
    result = subprocess.run(['curl', '-sS', '--connect-timeout', '10', '--max-time', '25',
                             '--write-out', '\n%{http_code}', url], capture_output=True, text=True)
    body, _, status = result.stdout.rpartition('\n')
    if result.returncode or status != '200':
        raise ValueError(f'Market source request failed: HTTP {status}, curl {result.returncode}')
    payload = json.loads(body)
    if payload.get('code') != 0:
        raise ValueError('Market source returned an unsuccessful response')
    instrument = payload['data'][symbol]
    field = 'qfqday' if adjustment else 'day'
    if field not in instrument:
        raise ValueError(f'Missing declared adjustment series {field}')
    rows = []
    for row in instrument[field]:
        values = [float(v) for v in row[1:6]]
        if len(values) != 5 or not all(math.isfinite(v) for v in values):
            raise ValueError('Invalid daily candle')
        o, c, h, l, v = values
        if not (0 < l <= min(o, c) <= max(o, c) <= h and v >= 0):
            raise ValueError('Daily candle violates OHLC bounds')
        rows.append(dict(date=row[0], open=o, close=c, high=h, low=l, volume=v))
    if [r['date'] for r in rows] != sorted(set(r['date'] for r in rows)):
        raise ValueError('Daily candle dates are not unique and ascending')
    return rows, url, started


def metrics(qfq, raw, name, code):
    if len(qfq) < 61 or len(raw) < 26 or qfq[-1]['date'] != raw[-1]['date']:
        raise ValueError('Not enough comparable daily history')
    last = qfq[-1]
    limit_pct = 19.5 if code.startswith(('300', '301', '688', '689')) else 9.5
    if 'ST' in name.upper():
        limit_pct = 4.8
    days = []
    for prev, row in zip(raw[:-1], raw[1:]):
        pct = (row['close'] / prev['close'] - 1) * 100
        candidate = pct >= limit_pct and row['close'] / row['high'] >= .998
        days.append({'date': row['date'], 'pct_change': pct, 'limit_like_close': candidate})
    trailing = 0
    for day in reversed(days):
        if not day['limit_like_close']:
            break
        trailing += 1
    ma20 = statistics.mean(r['close'] for r in qfq[-20:])
    volume_baseline = statistics.median(r['volume'] for r in qfq[-21:-1])
    if volume_baseline <= 0:
        raise ValueError('Not enough nonzero daily volume to calculate VR20')
    vr20 = last['volume'] / volume_baseline
    return {'last_date': last['date'], 'last_close': raw[-1]['close'],
            'return_5d_pct': (last['close'] / qfq[-6]['close'] - 1) * 100,
            'return_20d_pct': (last['close'] / qfq[-21]['close'] - 1) * 100,
            'ma20_extension_pct': (last['close'] / ma20 - 1) * 100,
            'from_60d_high_pct': (last['close'] / max(r['high'] for r in qfq[-60:]) - 1) * 100,
            'vr20': vr20, 'limit_like_20d': sum(d['limit_like_close'] for d in days[-20:]),
            'latest_limit_like_streak': trailing,
            'max_day_gain_20d_pct': max(d['pct_change'] for d in days[-20:]),
            'limit_like_dates_20d': [d['date'] for d in days[-20:] if d['limit_like_close']],
            'daily_change': days[-60:]}


PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>比赛热股与近期 K 线</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#202b3d;font:14px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}.wrap{max-width:1320px;margin:auto;padding:30px}h1{margin:0 0 8px;font-size:28px}h2{margin:0;font-size:20px}p{margin:6px 0}.muted{color:#687388}.notice{padding:14px 18px;background:#edf1ff;border-left:3px solid #3154c5;margin:18px 0}.tools{display:flex;gap:12px;align-items:center;margin:22px 0}button{border:1px solid #d7deeb;background:white;padding:8px 15px;border-radius:6px;cursor:pointer}button.active{background:#3154c5;color:white}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.card{padding:20px;background:white;border:1px solid #e4e8ef;border-radius:10px}.top{display:flex;justify-content:space-between;align-items:baseline;gap:10px}.facts{display:flex;flex-wrap:wrap;gap:8px 20px;font-size:12px;margin:12px 0}.facts b{font-size:15px}.positive{color:#c84545}.negative{color:#188667}svg{width:100%;height:auto;display:block}.comment{font-size:13px;color:#4f5d75;margin-top:12px}a{color:#3154c5;text-decoration:none}.sources{font-size:12px;margin-top:8px}table{width:100%;border-collapse:collapse;background:white;font-size:12px}td,th{padding:10px;border-bottom:1px solid #e4e8ef;text-align:right}td:first-child,th:first-child{text-align:left}.tablewrap{overflow:auto;margin:20px 0}footer{font-size:12px;color:#687388;margin-top:25px}@media(max-width:850px){.grid{grid-template-columns:1fr}.wrap{padding:18px}.top{display:block}}[hidden]{display:none!important}
</style><main class="wrap"><h1>比赛热股与近期 K 线</h1><p id="cutoff" class="muted"></p><div class="notice"><p>观察范围：官方热榜持仓人数合计前 10 只 A 股，剔除 ETF。人数按比赛相加，未跨比赛去重。</p><p>日 K 线采用前复权价格；“涨停形态候选”使用不复权涨幅与收盘接近最高价的组合，不等于官方涨停统计，也不能证明参赛者在涨停板买入。</p></div><div class="tablewrap"><table><thead><tr><th>股票</th><th>持仓人数合计</th><th>覆盖比赛</th><th>5日涨幅</th><th>20日涨幅</th><th>20日涨停形态候选</th><th>最新量比 VR20</th></tr></thead><tbody id="summary"></tbody></table></div><div class="tools"><span>显示交易日数</span><button data-days="20">20 日</button><button data-days="60" class="active">60 日</button><span class="muted">鼠标悬停 K 线可查看价格和成交量</span></div><div class="grid" id="charts"></div><footer><p>VR20 = 最新日成交量 / 前 20 个交易日成交量中位数。涨幅基于前复权收盘价；单日涨停形态候选基于不复权价格。</p><p>主板候选条件：单日涨幅 ≥9.5% 且收盘/最高价 ≥99.8%；ST、成长板使用对应近似门槛。除权日、历史风险警示状态、新股等可能影响判断。</p><p>本页仅描述这些股票的市场形态，持仓榜不包含买入价格、成交时间与实际持有周期。</p><a href="../index.html">返回比赛热股复盘</a></footer></main><script id="payload" type="application/json">__DATA__</script><script>
const report=JSON.parse(document.getElementById('payload').textContent), stocks=report.stocks;
const el=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e};
const fmt=(n,d=1)=>Number(n).toFixed(d),signed=n=>(n>0?'+':'')+fmt(n)+'%';
document.getElementById('cutoff').textContent='行情截至 '+report.metadata.latest_market_date+' 收盘 · 数据取得 '+report.metadata.fetched_at.replace('T',' ')+' · 行情来源：腾讯证券';
for(const stock of stocks){let r=el('tr');const m=stock.metrics;for(const value of [stock.stock_name+' '+stock.stock_code,stock.holder_count_sum,stock.contest_count,signed(m.return_5d_pct),signed(m.return_20d_pct),m.limit_like_20d,fmt(m.vr20,2)+'×'])r.appendChild(el('td',value));document.getElementById('summary').appendChild(r)}
const NS='http://www.w3.org/2000/svg';function shape(parent,tag,attrs,text){const node=document.createElementNS(NS,tag);for(const[k,v]of Object.entries(attrs))node.setAttribute(k,v);if(text!==undefined)node.textContent=text;parent.appendChild(node);return node}
function chart(stock,days){const rows=stock.qfq.slice(-days),svg=document.createElementNS(NS,'svg');svg.setAttribute('viewBox','0 0 600 305');svg.setAttribute('role','img');svg.setAttribute('aria-label',stock.stock_name+'近'+days+'个交易日日K线和成交量');const lo=Math.min(...rows.map(r=>r.low)),hi=Math.max(...rows.map(r=>r.high)),span=hi-lo||1,y=v=>22+(hi-v)/span*188,maxV=Math.max(...rows.map(r=>r.volume))||1,step=535/rows.length;
for(let i=0;i<=4;i++){const v=lo+span*i/4,py=y(v);shape(svg,'line',{x1:7,y1:py,x2:546,y2:py,stroke:'#edf0f5'});shape(svg,'text',{x:552,y:py+4,fill:'#687388','font-size':10},fmt(v,2))}
shape(svg,'text',{x:7,y:15,fill:'#687388','font-size':10},'价格（前复权）');shape(svg,'text',{x:7,y:235,fill:'#687388','font-size':10},'成交量');rows.forEach((r,i)=>{const x=7+(i+.5)*step,c=r.close>=r.open?'#d65050':'#229872',g=shape(svg,'g',{});shape(g,'title',{},`${r.date} 开${r.open} 高${r.high} 低${r.low} 收${r.close} 量${r.volume}`);shape(g,'line',{x1:x,y1:y(r.high),x2:x,y2:y(r.low),stroke:c,'stroke-width':1});shape(g,'rect',{x:x-step*.33,y:Math.min(y(r.open),y(r.close)),width:Math.max(1,step*.66),height:Math.max(1,Math.abs(y(r.open)-y(r.close))),fill:c});const height=r.volume/maxV*48;shape(g,'rect',{x:x-step*.33,y:290-height,width:Math.max(1,step*.66),height:height,fill:c,opacity:.75})});shape(svg,'text',{x:7,y:303,fill:'#687388','font-size':10},rows[0].date);shape(svg,'text',{x:535,y:303,'text-anchor':'end',fill:'#687388','font-size':10},rows.at(-1).date);return svg}
function comment(m){const parts=[];if(m.limit_like_20d)parts.push('最近20个交易日出现 '+m.limit_like_20d+' 次涨停形态候选'+(m.latest_limit_like_streak?'，最新连续 '+m.latest_limit_like_streak+' 日':'')+'。候选日期：'+m.limit_like_dates_20d.map(d=>d.slice(5)).join('、')+'。');else parts.push('最近20个交易日没有符合条件的涨停收盘形态。');parts.push('20日涨幅 '+signed(m.return_20d_pct)+'，最新成交量为此前20日中位数的 '+fmt(m.vr20,2)+' 倍。');if(m.latest_limit_like_streak>=2)parts.push('当前仍处于连续涨停式收盘阶段。');else if(m.return_5d_pct<=-5&&m.limit_like_20d>=2)parts.push('前期曾反复强涨，近5日已回撤 '+fmt(-m.return_5d_pct)+'%；应区分前期强势与当前调整。');else if(m.return_20d_pct<0&&m.return_5d_pct>=5)parts.push('近5日反弹，20日累计涨幅仍为负，呈回撤后修复形态。');else if(m.latest_limit_like_streak===1)parts.push('最新日出现涨停式收盘，但尚未构成当前连续形态。');else if(m.limit_like_20d>=2)parts.push('近期曾出现多次涨停式收盘，当前没有连续形态。');else parts.push('这段日线缺少反复涨停的特征。');parts.push('这些价格形态不能确认参赛者的买入时点。');return parts.join('')}
function render(days){const host=document.getElementById('charts');host.replaceChildren();for(const stock of stocks){const m=stock.metrics,card=el('section',undefined,'card'),top=el('div',undefined,'top');top.append(el('h2',stock.stock_name),el('span',stock.stock_code+' · '+stock.contest_count+'场比赛','muted'));card.appendChild(top);const facts=el('div',undefined,'facts');for(const[label,value]of [['最新收盘',fmt(m.last_close,2)],['5日涨幅',signed(m.return_5d_pct)],['20日涨幅',signed(m.return_20d_pct)],['20日涨停候选',m.limit_like_20d+'日']]){const item=el('span',label+' ');item.appendChild(el('b',value));facts.appendChild(item)}card.append(facts,chart(stock,days),el('p',comment(m),'comment'));const source=el('a','腾讯原始日K线数据');source.href=stock.sources.qfq;source.target='_blank';source.rel='noopener';const foot=el('p',undefined,'sources');foot.appendChild(source);card.appendChild(foot);host.appendChild(card)}}
for(const button of document.querySelectorAll('[data-days]'))button.addEventListener('click',()=>{for(const b of document.querySelectorAll('[data-days]'))b.classList.toggle('active',b===button);render(Number(button.dataset.days))});render(60);
</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analysis', type=Path, required=True)
    parser.add_argument('--cutoff', help='ISO cutoff date; defaults to cached market date or today for live requests')
    parser.add_argument('--candles', type=Path, help='Read an existing candles.json offline; never requests the network')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    analysis = json.loads(args.analysis.read_text(encoding='utf-8'))
    cached_metadata, cached_stocks = load_candles(args.candles) if args.candles else (None, None)
    cutoff = iso_date(args.cutoff or (cached_metadata['latest_market_date'] if cached_metadata
                                    else datetime.now(TZ).date().isoformat()), 'Cutoff date')
    candidates = [r for r in analysis['ranking'] if r['stock_code'].startswith(('0', '3', '6'))
                  and not any(term in r['stock_name'].upper() for term in ('ETF', 'LOF', 'REIT', '基金'))]
    candidates.sort(key=lambda r: (-r['holder_count_sum'], r['stock_code']))
    if not candidates:
        raise ValueError('The analysis does not contain any eligible equities')
    if cached_stocks is not None:
        missing = [row['stock_code'] for row in candidates[:10] if row['stock_code'] not in cached_stocks]
        if missing:
            raise ValueError('Cached candles are missing selected stocks: ' + ', '.join(missing))
    stocks, last_started = [], 0
    for row in candidates[:10]:
        code = row['stock_code']
        symbol = ('sh' if code.startswith('6') else 'sz') + code
        if cached_stocks is None:
            qfq, source_qfq, last_started = request(symbol, 'qfq', last_started)
            raw, source_raw, last_started = request(symbol, '', last_started)
        else:
            cached = cached_stocks[code]
            qfq, raw = cached['qfq'], cached['unadjusted']
            source_qfq, source_raw = cached['sources']['qfq'], cached['sources']['unadjusted']
        qfq = [r for r in qfq if r['date'] <= cutoff]
        raw = [r for r in raw if r['date'] <= cutoff]
        m = metrics(qfq, raw, row['stock_name'], code)
        stock = {k: row[k] for k in ('stock_code', 'stock_name', 'holder_count_sum', 'contest_count')}
        stock.update(symbol=symbol, qfq=qfq, unadjusted=raw, metrics=m,
                     sources={'qfq': source_qfq, 'unadjusted': source_raw})
        stocks.append(stock)
        print(json.dumps({'stock': row['stock_name'], **{k: m[k] for k in ('last_date','return_20d_pct','limit_like_20d','latest_limit_like_streak','vr20')}}, ensure_ascii=False), flush=True)
    dates = {s['metrics']['last_date'] for s in stocks}
    if len(dates) != 1:
        raise ValueError('Securities do not share a current market date; cannot present a uniform review')
    generated_at = datetime.now(TZ).isoformat(timespec='seconds')
    payload = {'metadata': {'fetched_at': cached_metadata['fetched_at'] if cached_metadata else generated_at,
                           'review_generated_at': generated_at,
                           'input_mode': 'offline-cache' if cached_metadata else 'live',
                           'cutoff_requested': cutoff, 'latest_market_date': dates.pop(),
                           'analysis_run_id': analysis['metadata']['run_id'],
                           'scope': 'Official hot-list holder-count top 10 equities, excluding funds',
                           'adjustment': ADJUSTMENT,
                           'source': SOURCE_NAME}, 'stocks': stocks}
    if cached_metadata:
        payload['metadata']['cached_latest_market_date'] = cached_metadata['latest_market_date']
        payload['metadata']['cached_cutoff_requested'] = cached_metadata.get('cutoff_requested')
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'candles.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    review = {'metadata': payload['metadata'],
              'stocks': [{k: v for k, v in stock.items() if k not in ('qfq', 'unadjusted')}
                         for stock in stocks]}
    (args.output / 'review.json').write_text(json.dumps(review, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    embedded = json.dumps(payload, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    (args.output / 'index.html').write_text(PAGE.replace('__DATA__', embedded), encoding='utf-8')


if __name__ == '__main__':
    main()
