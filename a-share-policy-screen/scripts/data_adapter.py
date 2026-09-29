# Copyright 2026 Simon Lin. Apache-2.0; see references/a-stock-data-LICENSE.txt.
# Modified: selected a-stock-data v3.10.0 read-only adapters, examples and unrelated APIs removed.
SH_INDEX = {'000300', '000905', '000016', '000688', '000852', '000010'}

def get_prefix(code: str) -> str:
    """6位代码 → 市场前缀（sh/sz/bj）。支持显式前缀/后缀（sh000016 / 000016.SH）透传以解决歧义。"""
    c = code.lower().strip()
    if c.endswith(('.sh', '.sz', '.bj')):
        return c[-2:]
    if c.endswith(('.xshg', '.xshe')):
        return 'sh' if c.endswith('.xshg') else 'sz'
    if c.startswith(('sh', 'sz', 'bj')):
        return c[:2]
    if c.startswith('92'):
        return 'bj'
    if c.startswith(('5', '6', '9')):
        return 'sh'
    if c.startswith(('4', '8')):
        return 'bj'
    if c in SH_INDEX:
        return 'sh'
    return 'sz'
import re
_TICKER_RE = re.compile('^(?:(sh|sz|bj)(\\d{6})|(\\d{6})(?:\\.(sh|sz|bj|xshg|xshe))?)$', re.IGNORECASE)
_JQ_SUFFIX = {'xshg': 'sh', 'xshe': 'sz'}

def _natural_market(digits: str) -> str:
    """6 位码的自然归属市场。仅用于校验显式前缀是否自相矛盾。
    注意 000xxx 是沪指数/深个股共用的歧义段，由调用处单独处理，不走这里。"""
    if digits.startswith(('4', '8', '92')):
        return 'bj'
    if digits[0] in ('5', '6', '9'):
        return 'sh'
    return 'sz'

def norm_ticker(code: str, stock_only: bool=False) -> str:
    """任意受支持写法 → 纯 6 位数字代码。

    支持 600519 / SH600519 / sh600519 / 600519.SH / BJ920982 等。
    stock_only=True：个股专用接口（研报、一致预期等）传这个，会拒绝显式指数写法。
    ⚠️ 不匹配时**抛 ValueError，绝不静默返回空串或猜一个代码**——
    否则调用方会把「代码格式写错」误读成「这只票没有数据」，
    或者更糟：拿到另一只股票的数据还以为是对的。
    """
    raw = str(code).strip()
    m = _TICKER_RE.match(raw)
    if not m:
        raise ValueError(f'无法把 {code!r} 解析为 6 位股票代码；支持格式：600519 / SH600519 / sh600519 / 600519.SH / 600519.XSHG（聚宽）（前缀与后缀二选一，不能同时写）')
    digits = m.group(2) or m.group(3)
    market = (m.group(1) or m.group(4) or '').lower()
    market = _JQ_SUFFIX.get(market, market)
    if market:
        if digits.startswith('000'):
            if market == 'bj':
                raise ValueError(f'{code!r} 市场标识与号段矛盾：000xxx 不属北交所。')
            if stock_only and market == 'sh':
                raise ValueError(f'{code!r} 指向沪市指数而非个股（沪市无 000xxx 个股），本接口只服务个股。要查同号段的深市个股请显式传 sz{digits}。')
        else:
            nat = _natural_market(digits)
            if market != nat:
                raise ValueError(f'{code!r} 的市场标识与号段矛盾：{digits} 属 {nat} 市，而不是 {market} 市。（改用 {nat}{digits} 或去掉市场标识）')
    return digits

def em_market_code(code: str) -> int:
    """东财 secid 的市场号：**沪=1，深/北=0**（V3.7.0 新增 · #46）。

    ⚠️ 绝不要用 `code.startswith("6")` 判市场 —— 那会把**沪市 ETF（51x）**、
    **科创板 ETF（588x）**、**沪 B 股（900x）** 全部错判成深市，接口返回 `data: null`。
    2026-08-19 实测：510300 / 588000 / 600519 / 688112 / 900901 → m=1；
    300750 / 159915 / 920982 / 832982 → m=0（北交所与深市共用 m=0）。
    """
    return 1 if get_prefix(code) == 'sh' else 0
import time
import random
import requests
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
DATACENTER_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
EM_SESSION = requests.Session()
EM_SESSION.headers.update({'User-Agent': UA})
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _em_adapter = HTTPAdapter(max_retries=Retry(total=3, connect=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET']))
    EM_SESSION.mount('https://', _em_adapter)
    EM_SESSION.mount('http://', _em_adapter)
except Exception:
    pass
from typing import Optional
EM_MIN_INTERVAL = 1.0
_em_last_call = [0.0]

def em_get(url: str, params: Optional[dict]=None, headers: Optional[dict]=None, timeout: int=15, **kwargs):
    """东财统一请求入口：自动节流 + 复用 session + 默认 UA。
    所有 eastmoney.com 接口都应通过它请求，避免高频被封 IP。"""
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return EM_SESSION.get(url, params=params, headers=headers, timeout=timeout, **kwargs)
    finally:
        _em_last_call[0] = time.time()
import functools
import math
import re
from datetime import date as _date_cls, datetime, timezone
import pandas as pd
import requests
V39_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'

def _v39_http(url, params=None, data=None, headers=None, method='GET', timeout=(10, 40), allow_status=(), allow_redirects=True):
    """非东财的 HTTP 请求：带浏览器 UA。网络错误、非 2xx 一律抛 RuntimeError（不把错误页当数据）；
    allow_status 里的状态码（源用 404 表示「当天没发布」时）原样返回，由调用方判断。"""
    merged = {'User-Agent': V39_UA}
    merged.update(headers or {})
    try:
        response = requests.request(method, url, params=params, data=data, headers=merged, timeout=timeout, allow_redirects=allow_redirects)
        if response.status_code not in allow_status:
            response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f'请求 {url} 失败: {type(exc).__name__}: {exc}') from exc
    return response

def _v39_json(response):
    """解析 JSON；不是 JSON 抛 RuntimeError。json 的解析错误是 ValueError 的子类，
    不转换会被调用方当成「确实没有数据」。"""
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{getattr(response, 'url', '')} 返回的不是 JSON，可能是错误页") from exc

def _v39_date(value):
    """'2026-09-18' / '20260918' / date 对象 → '2026-09-18'；其他写法抛 ValueError。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, _date_cls):
        return value.isoformat()
    text = str(value).strip()
    fmt = '%Y%m%d' if re.fullmatch('[0-9]{8}', text) else '%Y-%m-%d'
    return datetime.strptime(text, fmt).date().isoformat()

def _v39_src_date(value):
    """来源返回的日期 → 'YYYY-MM-DD'；认不出抛 RuntimeError（源格式变了，不是参数写错）。"""
    try:
        return _v39_date(value)
    except ValueError as exc:
        raise RuntimeError(f'来源返回了无法识别的日期 {value!r}') from exc

def _v39_num(value):
    """'1,234.50' → 1234.5；空串 / '-' / '--' / None → None；其他非数字抛 RuntimeError
    （来源给了认不出的值是「源的格式变了」，不能和参数错误的 ValueError 混在一起）。
    JSON 布尔值同样抛错：float(True)=1.0 会把格式错误静默写成价格 / 成交量。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise RuntimeError(f'来源在数值字段给了布尔值 {value!r}')
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (not math.isfinite(value)):
            return None
        return float(value)
    text = str(value).replace(',', '').strip()
    if text in ('', '-', '--', 'None', 'null'):
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise RuntimeError(f'来源返回了无法识别的数值 {value!r}') from exc
    return number if math.isfinite(number) else None

def _v39_rows(value, what):
    """来源里可能整段缺失的行列表：字段没有（None）按空处理，其余必须是对象列表。
    写成 `value or []` 会把 {} / '' / 0 这类结构改变也当成空表，静默丢掉整段数据。"""
    if value is None:
        return []
    if not isinstance(value, list) or not all((isinstance(row, dict) for row in value)):
        raise RuntimeError(f'{what} 应为对象列表，实际是 {type(value).__name__}: {str(value)[:120]}')
    return value

def _v39_labels(value, what):
    """来源的标签数组（频道名之类）：None 按空处理，其余必须是字符串列表。
    直接 `value or []` 再 join，来源把数组改成字符串时会被拆成单字（'ab' → 'a,b'）。"""
    if value is None:
        return []
    if not isinstance(value, list) or not all((isinstance(x, str) for x in value)):
        raise RuntimeError(f'{what} 应为字符串列表，实际是 {type(value).__name__}: {str(value)[:120]}')
    return value

def _v39_req_num(value, what):
    """必填数值（价格、成交量）：在 _v39_num 之上，空值 / NaN / inf 也抛 RuntimeError，不能当缺失放过。"""
    number = _v39_num(value)
    if number is None:
        raise RuntimeError(f'来源的 {what} 为空或不是有限数值: {value!r}')
    return number

def _v39_contract(func):
    """统一异常契约：来源行缺字段时 row["X"] 会漏出 KeyError，调用方按「参数错 / 没数据」处理就会
    把「来源格式变了」当成正常情况。这里把它转成带函数名和字段名的 RuntimeError。
    （函数内所有按用户参数取字典的地方都先校验过参数，不会走到这里。）"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except KeyError as exc:
            raise RuntimeError(f'{func.__name__}: 来源数据缺少字段 {exc}，格式可能已变') from exc
    return wrapper

def _v39_count(value, what):
    """来源自报的页数 / 条数 → 非负 int。只认 int 或纯数字串；bool 抛错（int(True)=1 会让
    「只返回 1 条」通过完整性核对），其他写法也抛 RuntimeError。"""
    text = str(value).strip() if isinstance(value, (int, str)) and (not isinstance(value, bool)) else ''
    if not re.fullmatch('[0-9]+', text):
        raise RuntimeError(f'{what} 不是非负整数: {value!r}')
    return int(text)

def _v39_frame(rows, source, url, columns=None):
    """统一出表：附 source / source_url / fetched_at。rows 为空时返回带列名的空表，
    是否允许为空由调用方判断（「确实没有」与「接口坏了」要分开处理）。"""
    frame = pd.DataFrame(rows, columns=columns)
    frame['source'] = source
    frame['source_url'] = url
    frame['fetched_at'] = datetime.now(timezone.utc).isoformat()
    return frame
import urllib.request

def tencent_quote(codes: list[str]) -> dict[str, dict]:
    """
    批量拉取腾讯财经实时行情。
    codes: ["688017", "300476", "002463"]
    也支持指数: ["000001", "000300", "399006"]
    也支持ETF: ["510050", "510300"]
    返回: {code: {name, price, pe_ttm, pb, mcap, ...}}
    """
    SH_INDEX = {'000300', '000905', '000016', '000688', '000852', '000010'}
    prefixed = []
    key_of = {}
    for c in codes:
        low = c.lower()
        if low.startswith(('sh', 'sz', 'bj')):
            p = low
        elif c.startswith('92'):
            p = f'bj{c}'
        elif c in SH_INDEX or c.startswith(('5', '6', '9')):
            p = f'sh{c}'
        elif c.startswith(('4', '8')):
            p = f'bj{c}'
        else:
            p = f'sz{c}'
        prefixed.append(p)
        key_of[p] = c
    url = 'https://qt.gtimg.cn/q=' + ','.join(prefixed)
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'Mozilla/5.0')
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode('gbk')
    result = {}
    for line in data.strip().split(';'):
        if not line.strip() or '=' not in line or '"' not in line:
            continue
        key = line.split('=')[0].split('_')[-1]
        vals = line.split('"')[1].split('~')
        if len(vals) < 53:
            continue
        code = key_of.get(key, key[2:])
        result[code] = {'name': vals[1], 'price': float(vals[3]) if vals[3] else 0, 'last_close': float(vals[4]) if vals[4] else 0, 'open': float(vals[5]) if vals[5] else 0, 'change_amt': float(vals[31]) if vals[31] else 0, 'change_pct': float(vals[32]) if vals[32] else 0, 'high': float(vals[33]) if vals[33] else 0, 'low': float(vals[34]) if vals[34] else 0, 'amount_wan': float(vals[37]) if vals[37] else 0, 'turnover_pct': float(vals[38]) if vals[38] else 0, 'pe_ttm': float(vals[39]) if vals[39] else 0, 'amplitude_pct': float(vals[43]) if vals[43] else 0, 'float_mcap_yi': float(vals[44]) if vals[44] else 0, 'mcap_yi': float(vals[45]) if vals[45] else 0, 'pb': float(vals[46]) if vals[46] else 0, 'limit_up': float(vals[47]) if vals[47] else 0, 'limit_down': float(vals[48]) if vals[48] else 0, 'vol_ratio': float(vals[49]) if vals[49] else 0, 'pe_static': float(vals[52]) if vals[52] else 0}
        q = result[code]
        q['is_stale'] = q['amount_wan'] == 0 and q['price'] == q['last_close'] and (q['price'] > 0)
        if q['is_stale'] and key[2:4] in ('43', '83', '87'):
            q['stale_reason'] = '北交所老号段，多数已迁至 920xxx，请按名称反查现行代码'
        elif q['is_stale']:
            q['stale_reason'] = '成交量为 0（停牌 / 未开盘 / 废码），报价非当日真实成交'
    return result
import time
from datetime import date, datetime, timedelta
TENCENT_KLINE_HOSTS = ['https://web.ifzq.gtimg.cn', 'https://proxy.finance.qq.com/ifzqgtimg', 'https://ifzq.gtimg.cn']
_TENCENT_HOST_COOLDOWN = 120
_tencent_host_down_until = {}
_TENCENT_MINUTES = ('m1', 'm5', 'm15', 'm30', 'm60')
_TENCENT_SPAN_DAYS = {'day': 700, 'week': 3650, 'month': 18250}

def _tencent_kline_call(path, param):
    """按顺序尝试三个入口；空响应或异常视为该入口被限流，冷却后换下一个。返回 (data, 实际成功的入口)。"""
    errors = []
    for host in TENCENT_KLINE_HOSTS:
        if _tencent_host_down_until.get(host, 0) > time.time():
            continue
        try:
            response = _v39_http(host + path, params={'param': param}, headers={'Referer': 'https://gu.qq.com/'}, timeout=(8, 20))
            payload = _v39_json(response) if response.text.strip() else {}
        except RuntimeError as exc:
            errors.append(f'{host}: {type(exc.__cause__ or exc).__name__}')
            _tencent_host_down_until[host] = time.time() + _TENCENT_HOST_COOLDOWN
            continue
        if not isinstance(payload, dict):
            errors.append(f'{host}: 顶层是 {type(payload).__name__}，不是对象')
            _tencent_host_down_until[host] = time.time() + _TENCENT_HOST_COOLDOWN
            continue
        if payload.get('msg') == 'param error':
            raise ValueError(f'腾讯 K 线参数错误（区间过长或代码不存在）: {param}')
        if isinstance(payload.get('data'), dict) and payload['data']:
            return (payload['data'], host)
        errors.append(f'{host}: 空响应')
        _tencent_host_down_until[host] = time.time() + _TENCENT_HOST_COOLDOWN
    raise RuntimeError('腾讯 K 线三个入口均不可用（可能被限流，稍后重试）: ' + '; '.join(errors))

@_v39_contract
def tencent_kline(code, period='day', adjust=None, start=None, end=None, count=320):
    """腾讯 K 线 — 日/周/月（默认前复权）与 1/5/15/30/60 分钟（不复权）。

    period: day / week / month / m1 / m5 / m15 / m30 / m60
    adjust: None=日周月默认 qfq、分钟默认不复权；可显式传 'qfq' / 'hfq' / ''（不复权）
    start/end: 仅日周月可用，'YYYY-MM-DD'；给了 start 会自动按段分页（单次最多 640 根）
    count: 不给 start 时取最近 count 根；日周月 ≤ 640，分钟 ≤ 320
    成交量保留供应商原值，可能为手或股；调用方须交叉核验单位。本接口没有成交额。
    不支持北交所：腾讯对北交所只返回最新 1 根日线，区间与分钟线为空（2026-09-20 实测），直接抛 ValueError。
    """
    period = str(period).lower()
    if get_prefix(code) == 'bj':
        raise ValueError('腾讯 K 线不支持北交所（只返回最新 1 根日线、分钟线为空）；北交所日线请用 §1.3 tdx_daily_package(date) 按交易日取')
    symbol = get_prefix(code) + norm_ticker(code)
    if period in _TENCENT_MINUTES:
        if adjust not in (None, ''):
            raise ValueError('分钟线只有不复权数据，adjust 请留空')
        if start or end:
            raise ValueError('分钟线只能取最近 count 根，不支持 start/end')
        if not 1 <= int(count) <= 320:
            raise ValueError('分钟线 count 范围 1–320')
        data, host = _tencent_kline_call('/appstock/app/kline/mkline', f'{symbol},{period},,{int(count)}')
        node = data.get(symbol)
        if not isinstance(node, dict) or not isinstance(node.get(period), list):
            raise RuntimeError(f'腾讯分钟线 {symbol} 的返回里没有 {period} 列表，格式可能已变')
        rows, seen = ([], set())
        try:
            for item in node[period]:
                stamp = datetime.strptime(item[0], '%Y%m%d%H%M')
                if stamp in seen:
                    raise RuntimeError(f'腾讯分钟线 {symbol} 同一时刻 {stamp} 出现两次，结果不可信')
                seen.add(stamp)
                rows.append({'datetime': stamp.strftime('%Y-%m-%d %H:%M'), 'open': _v39_req_num(item[1], 'open'), 'high': _v39_req_num(item[3], 'high'), 'low': _v39_req_num(item[4], 'low'), 'close': _v39_req_num(item[2], 'close'), 'volume': _v39_req_num(item[5], 'volume'), 'turnover_rate_pct': _v39_num(item[7]) / 100 if len(item) > 7 and _v39_num(item[7]) is not None else None})
        except (ValueError, TypeError, IndexError, KeyError) as exc:
            raise RuntimeError(f'腾讯分钟线 {symbol} 行格式改变: {exc}') from exc
        if not rows:
            raise RuntimeError(f'腾讯分钟线 {symbol} {period} 返回 0 根')
        frame = _v39_frame(rows, 'tencent', host + '/appstock/app/kline/mkline')
        frame.insert(0, 'code', symbol)
        return frame
    if period not in _TENCENT_SPAN_DAYS:
        raise ValueError('period 只能是 day/week/month 或 m1/m5/m15/m30/m60')
    adjust = 'qfq' if adjust is None else adjust
    if adjust not in ('qfq', 'hfq', ''):
        raise ValueError("adjust 只能是 'qfq' / 'hfq' / ''")
    if start:
        first = datetime.strptime(_v39_date(start), '%Y-%m-%d').date()
        last = datetime.strptime(_v39_date(end), '%Y-%m-%d').date() if end else date.today()
        if first > last:
            raise ValueError('start 不能晚于 end')
        windows, cursor = ([], first)
        while cursor <= last:
            stop = min(cursor + timedelta(days=_TENCENT_SPAN_DAYS[period] - 1), last)
            windows.append((cursor.isoformat(), stop.isoformat(), 640))
            cursor = stop + timedelta(days=1)
    else:
        if end:
            raise ValueError('只给 end 时请同时给 start')
        if not 1 <= int(count) <= 640:
            raise ValueError('日周月 count 范围 1–640')
        windows = [('', '', int(count))]
    by_date, used_hosts = ({}, [])
    for s, e, n in windows:
        data, host = _tencent_kline_call('/appstock/app/fqkline/get', f'{symbol},{period},{s},{e},{n},{adjust}')
        if host not in used_hosts:
            used_hosts.append(host)
        node = data.get(symbol)
        key = adjust + period
        if not isinstance(node, dict) or not isinstance(node.get(key, node.get(period)), list):
            raise RuntimeError(f"腾讯 K 线 {symbol} {s or '最近'}~{e or ''} 的返回里没有 {key} / {period}，格式可能已变")
        items = node.get(key, node.get(period))
        if key != period and key in node and (not items) and node.get(period):
            raise RuntimeError(f'腾讯 K 线 {symbol} {s}~{e} 的 {key} 为空、{period} 却有数据，不能拿原始价冒充复权价')
        try:
            for item in items:
                day = _v39_src_date(item[0])
                if s and (not s <= day <= e):
                    raise RuntimeError(f'腾讯 K 线 {symbol} 请求 {s}~{e} 却返回了 {day}，结果不可信')
                if day in by_date:
                    raise RuntimeError(f'腾讯 K 线 {symbol} 日期 {day} 出现两次，结果不可信')
                by_date[day] = {'date': day, 'open': _v39_req_num(item[1], 'open'), 'high': _v39_req_num(item[3], 'high'), 'low': _v39_req_num(item[4], 'low'), 'close': _v39_req_num(item[2], 'close'), 'volume': _v39_req_num(item[5], 'volume')}
        except (ValueError, TypeError, IndexError, KeyError) as exc:
            raise RuntimeError(f'腾讯 K 线 {symbol} 行格式改变: {exc}') from exc
    rows = [by_date[k] for k in sorted(by_date)]
    if start:
        rows = [r for r in rows if windows[0][0] <= r['date'] <= windows[-1][1]]
    if not rows:
        raise RuntimeError(f'腾讯 K 线 {symbol} {period} 在所给区间内 0 根（未上市/停牌区间/代码有误）')
    if any((min(r['open'], r['high'], r['low'], r['close']) <= 0 for r in rows)):
        raise RuntimeError(f"腾讯 {adjust or '原始'} 价格出现 ≤0（等差复权口径的副作用）；请改用 adjust='' 取不复权价，再用 §1.6 sina_adjust_factor + apply_adjust")
    frame = _v39_frame(rows, 'tencent', ' | '.join((h + '/appstock/app/fqkline/get' for h in used_hosts)))
    frame.insert(0, 'code', symbol)
    frame.insert(1, 'adjust', adjust or 'none')
    return frame
import requests

def eastmoney_stock_info(code: str) -> dict:
    """
    东财个股基本面信息。
    返回: {code, name, industry, total_shares, float_shares, mcap, float_mcap, list_date}
    """
    market_code = em_market_code(code)
    url = 'https://push2.eastmoney.com/api/qt/stock/get'
    params = {'fltt': '2', 'invt': '2', 'fields': 'f57,f58,f84,f85,f127,f116,f117,f189,f43', 'secid': f'{market_code}.{code}'}
    headers = {'User-Agent': UA}
    r = em_get(url, params=params, headers=headers, timeout=10)
    d = r.json().get('data', {})
    return {'code': d.get('f57', ''), 'name': d.get('f58', ''), 'industry': d.get('f127', ''), 'total_shares': d.get('f84', 0), 'float_shares': d.get('f85', 0), 'mcap': d.get('f116', 0), 'float_mcap': d.get('f117', 0), 'list_date': str(d.get('f189', '')), 'price': d.get('f43', 0)}

def eastmoney_concept_blocks(code: str) -> dict:
    """
    个股所属板块/概念归属（东财 slist，一次请求拿全，已内置限流）。
    返回: {total, boards: [{name, code(BK码), change_pct, lead_stock}], concept_tags: [板块名...]}
    boards 混合 行业/概念/地域，板块名自解释；concept_tags 是所有板块名的便捷列表。
    """
    market_code = em_market_code(code)
    params = {'fltt': '2', 'invt': '2', 'secid': f'{market_code}.{code}', 'spt': '3', 'pi': '0', 'pz': '200', 'po': '1', 'fields': 'f12,f14,f3,f128'}
    headers = {'User-Agent': UA, 'Referer': 'https://quote.eastmoney.com/'}
    try:
        r = em_get('https://push2.eastmoney.com/api/qt/slist/get', params=params, headers=headers, timeout=15)
        d = r.json()
    except Exception as e:
        print(f'[WARN] 东财板块归属请求失败: {e}')
        return {'total': 0, 'boards': [], 'concept_tags': []}
    diff = (d.get('data') or {}).get('diff') or {}
    items = diff.values() if isinstance(diff, dict) else diff
    boards = []
    for it in items:
        boards.append({'name': it.get('f14', ''), 'code': it.get('f12', ''), 'change_pct': it.get('f3', ''), 'lead_stock': it.get('f128', '')})
    return {'total': len(boards), 'boards': boards, 'concept_tags': [b['name'] for b in boards]}
from contextlib import contextmanager
import baostock as bs
import pandas as pd

@contextmanager
def bs_session():
    """baostock 登录会话 — 必须用上下文管理器，异常路径也保证 logout"""
    lg = bs.login()
    if lg.error_code != '0':
        raise RuntimeError(f'baostock 登录失败: {lg.error_code} {lg.error_msg}')
    try:
        yield
    finally:
        bs.logout()

def _rs_to_df(rs) -> pd.DataFrame:
    """baostock ResultData → DataFrame；错误码转异常，绝不静默返回空表"""
    if rs.error_code != '0':
        raise RuntimeError(f'baostock 查询失败: {rs.error_code} {rs.error_msg}')
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)

def _bs_code(code: str) -> str:
    """6位代码 → baostock 格式；北交所在登录前就拦掉"""
    code = str(code).zfill(6)
    if code[:2] in ('60', '68', '90'):
        return f'sh.{code}'
    if code[:2] in ('00', '30', '20'):
        return f'sz.{code}'
    raise ValueError(f'baostock 不支持该代码: {code}（北交所 4/8/92/920 号段会被服务端拒绝，报 10004011 股票代码未标识sh或sz）。北交所估值请改用 §1.1 腾讯当日快照。')

def baostock_stock_basic(code: str) -> dict:
    """标的基本信息 — ipoDate(上市日) / outDate(退市日，在市为空) / status(1=上市 0=退市)"""
    bs_code = _bs_code(code)
    with bs_session():
        df = _rs_to_df(bs.query_stock_basic(code=bs_code))
    return df.iloc[0].to_dict() if not df.empty else {}
import calendar
import math
import re
from datetime import datetime, timezone
from io import BytesIO
import pandas as pd
import requests

def _official_date(value):
    value = str(value).strip()
    fmt = '%Y%m%d' if re.fullmatch('[0-9]{8}', value) else '%Y-%m-%d'
    return datetime.strptime(value, fmt).date().isoformat()

def _official_get(url, params=None, referer=None):
    response = requests.get(url, params=params, headers={'User-Agent': 'Mozilla/5.0', 'Referer': referer or url}, timeout=(10, 40))
    response.raise_for_status()
    return response

def _official_frame(rows, keys, source, url):
    frame = pd.DataFrame(rows)
    if frame.empty or frame.duplicated(keys).any():
        raise RuntimeError('官方数据为空或主键重复，不能当成完整快照')
    frame['source'] = source
    frame['source_url'] = url
    frame['fetched_at'] = datetime.now(timezone.utc).isoformat()
    return frame.sort_values(keys).reset_index(drop=True)

def trading_calendar(year, month):
    """深交所完整自然月日历。未发布或缺日抛错，周末调休不视为交易日。"""
    if type(year) is not int or type(month) is not int or (not 1 <= month <= 12):
        raise ValueError('year/month 必须为整数，month 在 1–12 之间')
    last = calendar.monthrange(year, month)[1]
    expected = {datetime(year, month, day).date().isoformat() for day in range(1, last + 1)}
    url = 'https://www.szse.cn/api/report/exchange/onepersistenthour/monthList'
    response = _official_get(url, {'month': f'{year}-{month}'})
    data = response.json().get('data')
    if not isinstance(data, list) or not data:
        raise RuntimeError('深交所尚未返回该月日历；不能推断全月休市')
    rows = []
    for rec in data:
        if str(rec.get('jybz')) not in ('0', '1') or not rec.get('jyrq'):
            raise RuntimeError('深交所日历字段异常')
        rows.append({'date': _official_date(rec['jyrq']), 'is_open': str(rec['jybz']) == '1'})
    frame = _official_frame(rows, ['date'], 'szse', response.url)
    if set(frame.date) != expected:
        raise RuntimeError('日历月份错位或日期不完整，不能继续调度')
    return frame
