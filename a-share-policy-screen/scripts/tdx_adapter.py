# Copyright 2026 Simon Lin. Apache-2.0; see references/a-stock-data-LICENSE.txt.
# Modified: selected a-stock-data v3.10.0 read-only adapters, examples and unrelated APIs removed.
import socket
from mootdx.quotes import Quotes
_TDX_SERVERS = [('119.97.185.59', 7709), ('124.70.133.119', 7709), ('116.205.183.150', 7709), ('123.60.73.44', 7709), ('116.205.163.254', 7709), ('121.36.225.169', 7709), ('123.60.70.228', 7709), ('124.71.9.153', 7709), ('110.41.147.114', 7709), ('124.71.187.122', 7709)]

def _probe(ip, port, timeout=2.0):
    """TCP 握手探测（快速粗筛）。注意：握手成功 ≠ 能取数，必须再经 _validate 验活。"""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False

def _validate(client, market: str='std', check: str='bars') -> bool:
    """真实取数验活：坏服务器可 TCP 握手通过却回 2 字节空 body → 静默空表。用一次真实请求兜底。

    check='bars'   ：用 K 线请求验活（§1.7 行情类调用）；
    check='finance'：财务快照、F10 类别表里的「最新提示」及其正文都要取到才算活（§6.1 财务 / §6.2、§7.2 F10 调用），
                     只验财务会选中「财务正常、F10 为空」的服务器，F10 随后静默给空文本；
                     只看类别表非空，又会放过只回别的类别或畸形对象的服务器。
    两者要分开：2026-09 起通达信公开服务器的行情类命令（bars / quotes / transaction）普遍返回空表，
    而 finance / F10「最新提示」/ 除权除息仍正常（#52）。财务类调用若仍用 K 线验活，会被误判成「全部不可达」。
    验活样本 '000001' 是 A 股代码，只对 market='std' 有意义。其它市场（如扩展行情 'ext'）
    用它必然取不到数，会把所有正常服务器都判死、误报「全部不可达」，故非 std 时跳过验活。
    """
    if market != 'std':
        return True
    try:
        if check == 'finance':
            cats = client.F10C(symbol='000001')
            if not any((isinstance(c, dict) and c.get('name') == '最新提示' for c in cats)):
                return False
            text = client.F10(symbol='000001', name='最新提示')
            if not isinstance(text, str) or not text.strip():
                return False
            df = client.finance(symbol='000001')
        else:
            df = client.bars(symbol='000001', frequency=9, offset=1)
        return df is not None and (not df.empty)
    except Exception:
        return False

def tdx_client(market='std', check='bars'):
    """
    创建 mootdx 客户端，规避 0.11.x BESTIP.HQ 空串 bug + 坏服务器静默空表（#43）。
    check: 'bars'（默认，K 线 / 盘口 / 逐笔）或 'finance'（财务快照 / F10），决定用哪类请求验活（#52）。
    每个候选都必须「真实取数验活」通过才采用（_probe TCP 握手是假阳性来源）：
      1) 顺序探测 _TDX_SERVERS，对 probe 通过者再 _validate 真实取数，取第一个验活成功的；
      2) 全部失败 → 回退 mootdx 自带 bestip 测速选优（同样验活）；
      3) 再回退裸 factory（老用户 config 已有可用 BESTIP 时成立）；
      4) 仍失败 → 抛 RuntimeError，明确报错而非静默返回空表 / 崩溃。
    """
    if check not in ('bars', 'finance'):
        raise ValueError("check 只能是 'bars' 或 'finance'")
    for ip, port in _TDX_SERVERS:
        if not _probe(ip, port):
            continue
        try:
            c = Quotes.factory(market=market, server=(ip, port))
            if _validate(c, market, check):
                return c
        except Exception:
            continue
    for kwargs in ({'bestip': True}, {}):
        try:
            c = Quotes.factory(market=market, **kwargs)
            if _validate(c, market, check):
                return c
        except Exception:
            continue
    hint = '海外网络通常全部超时（TCP 7709），请走国内代理或更新 _TDX_SERVERS 列表。'
    if check == 'bars':
        hint += "若国内网络也如此：2026-09 起通达信公开服务器的 K 线 / 盘口 / 逐笔命令普遍返回空（#52），K 线改用 §1.2 tencent_kline()（日周月 + 1~60 分钟）或 §1.3 tdx_daily_package()（全市场当日日线含成交额），逐笔改用 §1.4 tencent_ticks()（当日）；财务与 F10 用 tdx_client(check='finance') 仍可取。"
    raise RuntimeError('所有 mootdx 服务器均无法取到数据（TCP 可达但返回空 / 被 reset）。' + hint)
