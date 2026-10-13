"""Build the approved strategy-tab / compact-table report from any valid screen run."""
import html
import base64
import gzip
import time
import json
import math
from pathlib import Path
from statistics import mean
from policies import aggregate_iso_weeks

NAMES = {'LOW_VOLUME_STABLE_PRICE':'地量','B1':'B1','B1_WEEKLY':'B1 周线','B1_V2':'B1_V2',
         'MA_GOLDEN_CROSS_FIRST_PULLBACK':'年线金叉回踩','LOG_BIAS_STRENGTH_FILTER':'对数乖离率'}
ASSET = Path(__file__).resolve().parents[1] / 'assets' / 'report-fragment.html'


def pct(a, b):
    return (a / b - 1) * 100 if a is not None and b else None


def ma(rows, n, end):
    return mean(r['close'] for r in rows[end-n:end]) if end >= n else None


def profile(rows, unit):
    if not rows:
        return None
    last = rows[-1]
    chart = []
    prefix = [0.0]
    for row in rows:
        prefix.append(prefix[-1] + row['close'])
    for i in range(max(0, len(rows)-60), len(rows)):
        b = rows[i]
        chart.append({'date': b['date'], **{k:b[k] for k in ['open','high','low','close']}, 'volume': b['volume']/10000,
                      **{f'ma{n}':(prefix[i+1]-prefix[i+1-n])/n if i+1>=n else None for n in [5,10,20,60,120,250,480]}})
    return {'unit':unit, 'date':last['date'], 'close':last['close'], 'chart':chart,
            'return_pct':pct(last['close'],rows[-2]['close']) if len(rows)>1 else None,
            'return5':pct(last['close'],rows[-6]['close']) if len(rows)>5 else None,
            'return20':pct(last['close'],rows[-21]['close']) if len(rows)>20 else None,
            'volume_change':pct(last['volume'],rows[-2]['volume']) if len(rows)>1 else None,
            'volume_wan':last['volume']/10000,
            'volume5':last['volume']/mean(r['volume'] for r in rows[-6:-1]) if len(rows)>5 else None,
            'week_days':last.get('trading_days')}


def scalar(value):
    return value if not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value) else None


def view_model(report):
    stocks = {}
    candidates = {sid for p in report['policies'] for sid in p['hits']}
    for sid, s in report['stocks'].items():
        if sid not in candidates or s.get('errors') or not s.get('bars'):
            continue
        boards = s.get('boards') or {}
        quote = s.get('quote') or {}
        current_quote = quote.get('date') == report['target_date']
        members = boards.get('members', boards.get('boards', []))
        # Mixed provider labels do not become invented industry/concept classifications.
        industry = boards.get('industries') or [boards.get('industry') or '行业待补']
        themes = boards.get('concepts') or boards.get('concept_tags', [])[:3]
        stocks[sid] = {'symbol':sid, 'code':sid[2:]+'.'+sid[:2].upper(), 'name':s.get('name') or sid,
             'amount_yi':scalar(quote.get('amount_yi')) if current_quote else None,
             'turnover':scalar(quote.get('turnover_pct')) if current_quote else None,
             'industries':industry, 'themes':themes,
             'boards':[{'name':b['name'], 'return_pct':scalar(b.get('change_pct')) if b.get('date',boards.get('quote_date')) == report['target_date'] else None}
                       for b in members if isinstance(b,dict) and b.get('name')],
             'daily':profile(s['bars'],'日'), 'weekly':profile(s.get('_weeks') or aggregate_iso_weeks(s['bars']),'周')}
    scope = '部分样本' if report['scope']=='subset' else '当前活跃沪深 A 股池'
    if report['scope']=='subset' and report.get('universe_source','').endswith('; user-requested SH/SZ MAIN filter'):
        scope = '沪深主板'
    policies = [{**{k:p[k] for k in ['id','status','pool','evaluated','missing','hit_count','hits']},
                 'name':NAMES[p['id']], 'period':'weekly' if p['id']=='B1_WEEKLY' else 'daily',
                 'blocked':False, 'reason':p.get('reason','')} for p in report['policies']]
    for policy, source in zip(policies, report['policies']):
        policy['ranked'] = source['id']=='B1' and source.get('version','').startswith('b1_volume_contraction_v')
        policy['version'] = source.get('version')
        policy['shapes'] = {}
        if policy['ranked']:
            for row in source['results']:
                if row['status'] != 'hit':
                    continue
                m = row['metrics']; signal = m['selected_signal']
                policy['shapes'][row['symbol']] = {
                    'score':m['shape_score'], 'signal_date':m['signal_date'],
                    'age_bars':signal['age_bars'], 'volume_multiple':signal['volume_multiple'],
                    'contraction_days':signal['contraction_days'],
                    'mean_volume_fraction':signal['mean_contraction_fraction'],
                    'contraction_dates':signal['contraction_dates']}
    notes = [f'候选池：{report["universe_source"]}；交易日历：{report["calendar_source"]}。',
             f'生成时间：{report["generated_at"]}。最后 K 线必须等于目标日；各策略独立统计。',
             '行情与股本来源：'+json.dumps(report.get('sources',{}),ensure_ascii=False),
             '使用当前活跃资产池、最新股本与板块，历史日期结果不等同无前视偏差回测。',
             '前5日均量不含当日；成交量统一为手后显示万手；MA与收益率使用同一前复权序列。',
             '周K包含未结束本周；表格使用日行情，图表可切换日/周。缺失的行业、概念、换手率及成交额不补造。',
             '策略命中仅为规则成立，不是交易指令或收益预测。']
    if any(p['ranked'] for p in policies):
        versions='、'.join(p['version'] for p in policies if p['ranked'])
        notes.append('日线B1公式版本：'+versions+'；形态分0—100，仅衡量图形贴合程度，不是上涨概率，不与其他策略混合评分。历史结果沿用保存时的公式与评分。')
    return {'date':report['target_date'],'previous_market_day':report['previous_market_day'],
            'scope_label':f'{scope} · {len(report["stocks"])} 只', 'stocks':stocks,'policies':policies,
            'notes':notes}


def escaped(value):
    if isinstance(value,str):
        return html.escape(value, quote=True)
    if isinstance(value,list):
        return [escaped(v) for v in value]
    if isinstance(value,dict):
        return {k:escaped(v) for k,v in value.items()}
    return value


def text_num(value, suffix=''):
    return '—' if value is None else f'{value:.2f}'+suffix


def markdown(report, model):
    lines = [f'# 选股结果 · {report["target_date"]}', '', model['scope_label'], '']
    for p in model['policies']:
        lines += [f'## {p["name"]} · {p["id"]}', '']
        if p['blocked']:
            lines += ['来源不可用；候选池和入选数未产生。'+p['reason'], '']
            continue
        lines += [f'状态 {p["status"]}｜候选池 {p["pool"]}｜已评估 {p["evaluated"]}｜未评估 {p["missing"]}｜入选 {p["hit_count"]}', '']
        if not p['hits']:
            lines += ['本次无入选股票。' if p['status']=='completed' else '未产生入选股票；覆盖不足，不能视为全量成功零候选。','']
            continue
        if p['ranked']:
            lines += ['形态分按高到低排列，表示图形贴合程度，不是上涨概率。', '']
        lines += ['| 股票 | '+('形态分 | 异动日 | ' if p['ranked'] else '')+'最后K日期 | 收盘 | 日涨跌% | 成交量(万手) | 成交额(亿) | 量/前5日均量 | 换手% | 行业 / 标签 |',
                  '|---|'+('---:|---|' if p['ranked'] else '')+'---|---:|---:|---:|---:|---:|---:|---|']
        for sid in p['hits']:
            s=model['stocks'][sid];d=s['daily']
            cells=[s['name']+' '+s['code'],d['date'],text_num(d['close']),text_num(d['return_pct']),text_num(d['volume_wan']),
                   text_num(s['amount_yi']),text_num(d['volume5']),text_num(s['turnover']),' / '.join(s['industries']+s['themes'])]
            if p['ranked']:
                shape = p['shapes'][sid]
                cells[1:1] = [text_num(shape['score']), shape['signal_date']]
            lines.append('| '+' | '.join(str(v).replace('|','／').replace('\n',' ') for v in cells)+' |')
        lines.append('')
    lines += ['## 数据来源与口径','',*model['notes'],'','MA 仅展示在联动量价图中，详细计算留存在 results.json。','']
    return '\n'.join(lines)


CHART_COLUMNS = ['date','open','high','low','close','volume','ma5','ma10','ma20','ma60','ma120','ma250','ma480']


def pack_model(model):
    dates = sorted({r['date'] for s in model['stocks'].values() for period in ('daily','weekly') for r in s[period]['chart']})
    positions = {day:i for i,day in enumerate(dates)}
    stocks = {}
    for sid, stock in model['stocks'].items():
        value = dict(stock)
        for period in ('daily','weekly'):
            value[period] = {**stock[period], 'chart':[[positions[r['date']],*[r[k] for k in CHART_COLUMNS[1:]]]
                                                    for r in stock[period]['chart']]}
        stocks[sid] = value
    return {**model,'stocks':stocks,'chart_columns':CHART_COLUMNS,'chart_dates':dates}


def write_reports(report, out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    started=time.perf_counter();model=view_model(report)
    model_seconds=time.perf_counter()-started
    content=ASSET.read_text()
    if content.count('__DATA_JSON__')!=1:
        raise ValueError('template_data_marker_invalid')
    packed=pack_model(escaped(model))
    raw=json.dumps(packed,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    # Small reports stay plain JSON for inexpensive inline use and broad compatibility.
    compressed=gzip.compress(raw,compresslevel=6,mtime=0)
    data=raw.decode().replace('</','<\\/') if len(raw)<500_000 else json.dumps({
        'encoding':'gzip-base64','payload':base64.b64encode(compressed).decode()},separators=(',',':'))
    (out/'view-model.json.gz').write_bytes(compressed)
    content=content.replace('__DATA_JSON__',data).replace('__TARGET_DATE__',html.escape(model['date'])).replace('__SCOPE_LABEL__',html.escape(model['scope_label']))
    inline_available=len(content.encode('utf-8'))<1_000_000
    if inline_available:
        (out/'report.inline.html').write_text(content)
    shell='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>选股结果</title><style>body{margin:16px auto;max-width:1100px;padding:0 12px}button{cursor:pointer}</style></head><body>'
    (out/'report.html').write_text(shell+content+'</body></html>')
    (out/'report.md').write_text(markdown(report,model))
    (out/'render-manifest.json').write_text(json.dumps({'inline_available':inline_available,
        'fragment_bytes':len(content.encode('utf-8')),'candidate_count':len(model['stocks']),
        'model_seconds':model_seconds,'total_seconds':time.perf_counter()-started,
        'raw_payload_bytes':len(raw),'compressed_bytes':len(compressed),
        'fallback':'report.md contains the full candidate list; HTML uses local gzip decoding for large reports'},ensure_ascii=False,indent=2))
