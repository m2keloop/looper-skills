"""Independent implementation of the seven user-supplied policies, not the Go service.

Only raw data come from a-stock-data. Formula windows are reset for each policy.
All dates are actual bar dates; invalid/missing input never becomes a zero-hit success.
"""
import math
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

POLICIES = ["LOW_VOLUME_STABLE_PRICE", "B1", "B1_WEEKLY", "B1_V2", "B2_V1",
            "MA_GOLDEN_CROSS_FIRST_PULLBACK", "LOG_BIAS_STRENGTH_FILTER"]
WINDOWS = dict(zip(POLICIES, [120, 21, 21, 114, 9, 741, 120]))


def validate(rows, n, target):
    if len(rows) < n:
        raise ValueError(f"insufficient_bars:{len(rows)}<{n}")
    bars = rows[-n:]
    date.fromisoformat(target)
    for bar in bars:
        date.fromisoformat(bar['date'])
    if bars[-1]["date"] != target:
        raise ValueError("stale_last_bar:" + bars[-1]["date"])
    if any(a["date"] >= b["date"] for a, b in zip(bars, bars[1:])):
        raise ValueError("unordered_or_duplicate_dates")
    for b in bars:
        for f in ("open", "high", "low", "close", "volume"):
            v = b.get(f)
            if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0:
                raise ValueError("invalid_" + f + ":" + b["date"])
        if b["high"] < max(b["open"], b["close"], b["low"]) or b["low"] > min(b["open"], b["close"]):
            raise ValueError("inconsistent_ohlc:" + b["date"])
    return bars


def ema(values, n):
    result = [values[0]]
    a = 2 / (n + 1)
    for v in values[1:]:
        result.append(a * v + (1 - a) * result[-1])
    return result


def sma(values, n):
    return [None if i < n - 1 else math.fsum(values[i - n + 1:i + 1]) / n for i in range(len(values))]


def kdj(rows):
    k = d = 50.0
    for i, bar in enumerate(rows):
        w = rows[max(0, i - 8):i + 1]
        low, high = min(b["low"] for b in w), max(b["high"] for b in w)
        rsv = 50.0 if high == low else 100 * (bar["close"] - low) / (high - low)
        k = (2 * k + rsv) / 3
        d = (2 * d + k) / 3
    return {"K": k, "D": d, "J": 3 * k - 2 * d}


def yellow_white(rows):
    closes = [b["close"] for b in rows]
    return {"white": ema(ema(closes, 10), 10)[-1],
            "yellow": math.fsum(math.fsum(closes[-n:]) / n for n in (14, 28, 57, 114)) / 4}


def aggregate_iso_weeks(rows):
    result = []
    last_key = None
    for b in rows:
        key = date.fromisoformat(b["date"]).isocalendar()[:2]
        if key != last_key:
            result.append({k: b[k] for k in ("date", "open", "high", "low", "close", "volume")})
            result[-1].update(first_date=b["date"], iso_week=f"{key[0]}-W{key[1]:02}", trading_days=1)
        else:
            w = result[-1]
            w.update(date=b["date"], high=max(w["high"], b["high"]), low=min(w["low"], b["low"]),
                     close=b["close"], volume=w["volume"] + b["volume"], trading_days=w["trading_days"] + 1)
        last_key = key
    return result


def equity_gate(rows, finance, boards):
    missing = []
    shares = finance.get("total_shares") if finance else None
    if isinstance(shares, bool) or not isinstance(shares, (int, float)) or not math.isfinite(shares) or shares <= 0:
        missing.append("missing_total_shares")
    tags = boards.get("concept_tags") if boards else None
    listing = boards.get('listing_board') if boards else None
    if listing not in ('MAIN', 'CHINEXT', 'STAR') and (not isinstance(tags, list) or not tags):
        missing.append("missing_board_membership")
    excluded = (listing in ('CHINEXT', 'STAR')) if listing in ('MAIN', 'CHINEXT', 'STAR') else None if not tags else any("创业板" in x or "科创板" in x for x in tags)
    cap = None if "missing_total_shares" in missing else rows[-1]["close"] * shares / 10000
    return {"market_cap_wan": cap, "excluded_board": excluded, "missing": missing}


def finish(checks, metrics, missing=None):
    if missing:
        return {"status": "not_evaluated", "reasons": missing, "checks": checks, "metrics": metrics}
    return {"status": "hit" if all(checks.values()) else "not_hit", "checks": checks, "metrics": metrics}


def surge(bars, i):
    return bars[i]["volume"] >= bars[i-1]["volume"] * 2 and bars[i]["close"] > bars[i-1]["close"]


def run_policy(policy, rows, target, finance=None, boards=None, yellow_filter=False, require_pullback=False):
    if policy == "B2_V1":
        raise ValueError("B2 requires run_b2 with a saved source snapshot")
    need = 114 if yellow_filter and policy in ("B1", "B1_WEEKLY") else WINDOWS[policy]
    try:
        if policy == 'B1_WEEKLY':
            validate(rows, len(rows) or 1, target)
        data = aggregate_iso_weeks(rows) if policy == "B1_WEEKLY" else rows
        bars = validate(data, need, target)
    except (ValueError, KeyError, TypeError) as e:
        return {"status": "not_evaluated", "reasons": [str(e)]}
    close, volume = bars[-1]["close"], bars[-1]["volume"]
    common = {"last_bar_date": bars[-1]["date"], "window": need, "close": close}
    if policy in ("LOW_VOLUME_STABLE_PRICE", "B1", "B1_WEEKLY"):
        gate = equity_gate(bars, finance, boards)
        common.update({k: v for k, v in gate.items() if k != "missing"})
        checks = {"market_cap_at_least_80_yi": None if gate["market_cap_wan"] is None else gate["market_cap_wan"] >= 800000,
                  "board_allowed": None if gate["excluded_board"] is None else not gate["excluded_board"]}
        if policy == "LOW_VOLUME_STABLE_PRICE":
            vmin = min(b["volume"] for b in bars)
            count = sum(b["volume"] <= 1.3 * vmin for b in bars[-5:])
            avg = math.fsum(b["close"] for b in bars[-5:]) / 5
            deviation = max(abs(b["close"] / avg - 1) for b in bars[-5:])
            checks.update(low_volume_days=count >= 3, price_stable=all(avg * .97 <= b["close"] <= avg * 1.03 for b in bars[-5:]))
            common.update(vmin=vmin, low_volume_days=count, max_deviation_pct=deviation * 100)
        else:
            indicator = kdj(bars)
            events = [{"date": bars[i]["date"], "volume_multiple": bars[i]["volume"] / bars[i-1]["volume"]}
                      for i in range(len(bars)-20, len(bars)) if surge(bars, i)]
            bad = [bars[i]["date"] for i in range(len(bars)-5, len(bars)) if bars[i]["close"] <= bars[i-1]["close"] * .905]
            checks.update(j_below_15=indicator["J"] < 15, two_surges=len(events) >= 2, no_drop_9_5=not bad)
            common.update(**indicator, surge_count=len(events), surges=events, large_drop_dates=bad)
            if policy == "B1_WEEKLY":
                common.update(latest_week=bars[-1]["iso_week"], latest_week_trading_days=bars[-1]["trading_days"],
                              provisional_week=date.fromisoformat(target).weekday() < 4)
            if yellow_filter:
                yw = yellow_white(bars)
                checks.update(white_above_yellow=yw["white"] > yw["yellow"], close_above_yellow=close > yw["yellow"])
                common.update(**yw)
        return finish(checks, common, gate["missing"])
    if policy == "B1_V2":
        yw, indicator = yellow_white(bars), kdj(bars)
        n = len(bars)
        lows = [{"date": bars[i]["date"], "confirmation_date": bars[i+1]["date"], "close": bars[i]["close"]}
                for i in range(n-61, n-1)
                if bars[i]["close"] < bars[i-1]["close"] and bars[i+1]["close"] > bars[i]["close"]]
        events = [i for i in range(n-21, n-3) if surge(bars, i)]
        s = events[-1] if events else None
        ret = (close / bars[-2]["close"] - 1) * 100
        amplitude = (bars[-1]["high"] - bars[-1]["low"]) / bars[-2]["close"] * 100
        checks = {"white_above_yellow": yw["white"] > yw["yellow"], "close_above_yellow": close > yw["yellow"],
                  "j_below_zero": indicator["J"] < 0, "rising_lows": len(lows) >= 2 and lows[-1]["close"] > lows[-2]["close"],
                  "contracted_volume": s is not None and volume <= .5 * bars[s]["volume"],
                  "return_range": bars[-2]["close"] * .98 <= close <= bars[-2]["close"] * 1.018,
                  "amplitude": bars[-1]["high"] - bars[-1]["low"] <= bars[-2]["close"] * .07}
        common.update(**yw, **indicator, confirmed_lows=lows[-2:], surge_date=bars[s]["date"] if s is not None else None,
                      volume_fraction=volume / bars[s]["volume"] if s is not None else None, return_pct=ret, amplitude_pct=amplitude)
        return finish(checks, common)
    if policy == "MA_GOLDEN_CROSS_FIRST_PULLBACK":
        closes = [b["close"] for b in bars]
        ma = {n: sma(closes, n) for n in (120, 250, 480)}
        crosses = []
        for i in range(len(bars)-260, len(bars)):
            for fast, slow in ((250, 480), (120, 250)):
                if ma[fast][i-1] <= ma[slow][i-1] and ma[fast][i] > ma[slow][i]:
                    crosses.append((i, fast, slow))
        cross = crosses[-1] if crosses else None
        drawdown = None if cross is None else (max(closes[cross[0]:])-close) / max(closes[cross[0]:])
        support = []
        for n in (120, 250):
            touched = [bars[i]["date"] for i in range(len(bars)-5, len(bars))
                       if bars[i]["low"] <= ma[n][i] * 1.03 and bars[i]["close"] >= ma[n][i] * .97]
            if touched and ma[n][-1] <= close <= ma[n][-1] * 1.12:
                support.append({"ma": n, "value": ma[n][-1], "touched_dates": touched, "close_multiple": close / ma[n][-1]})
        vr = volume / (math.fsum(b["volume"] for b in bars[-6:-1]) / 5)
        checks = {"ma_order": ma[120][-1] > ma[250][-1] > ma[480][-1],
                  "prior_golden_cross": cross is not None and cross[0] < len(bars)-1,
                  "drawdown_at_least_5pct": drawdown is not None and drawdown >= .05,
                  "support": bool(support), "volume_ratio_at_least_1_5": vr >= 1.5}
        common.update(ma120=ma[120][-1], ma250=ma[250][-1], ma480=ma[480][-1],
                      cross_date=bars[cross[0]]["date"] if cross else None,
                      cross_pair=list(cross[1:]) if cross else None, drawdown=drawdown, support=support, volume_ratio=vr)
        return finish(checks, common)
    if policy == "LOG_BIAS_STRENGTH_FILTER":
        x = [math.log(b["close"]) for b in bars]
        bias = [100 * (a-b) for a,b in zip(x, ema(x, 20))]
        state = 0
        for b in bias[-60:-1]:
            if state == 0 and b >= 5:
                state = 1
            elif state == 1 and abs(b) <= 1:
                state = 2
        pullback = state == 2 and bias[-1] >= 5
        checks = {"bias_range": -5 <= bias[-1] <= 15}
        if require_pullback:
            checks["pullback"] = pullback
        common.update(bias=bias[-1], strong_zone=bias[-1] >= 5, pullback=pullback)
        return finish(checks, common)
    raise ValueError("unknown_policy:" + policy)


def b2_component(rows, target, signal_date):
    bars = validate(rows, 9, target)
    if [b["date"] for b in bars[-2:]] != [signal_date, target]:
        raise ValueError("B2 requires adjacent actual signal and target bars")
    b, s = bars[-1], bars[-2]
    indicator = kdj(bars)
    ret = (b["close"] / s["close"] - 1) * 100
    checks = {"positive_candle": b["close"] > b["open"], "gain_over_4pct": b["close"] > s["close"] * 1.04,
              "higher_volume": b["volume"] > s["volume"], "j_below_55": indicator["J"] < 55}
    return finish(checks, {**indicator, "signal_date": signal_date, "last_bar_date": target,
                           "return_pct": ret, "volume_multiple": b["volume"] / s["volume"],
                           "no_upper_shadow": b["high"] == b["close"]})


def run_b2(snapshot, rows_by_symbol, target, previous_market_day, strict_next_day=False):
    if (not isinstance(snapshot,dict) or snapshot.get("status") != "success" or snapshot.get("policy_id") != "B1_V2"
            or snapshot.get("version") != "b1_yellow_white_v2.0" or snapshot.get("trade_date") != previous_market_day
            or not isinstance(snapshot.get("candidates"), list) or not snapshot.get("saved_at")):
        return {"status": "source_unavailable", "reason": "B2 来源不可用：缺少已成功保存的前一交易日指定版本 B1_V2 快照", "hit_count": None}
    try:
        saved = datetime.fromisoformat(snapshot['saved_at'].replace('Z','+00:00'))
        start = datetime.combine(date.fromisoformat(previous_market_day),time(15),ZoneInfo('Asia/Shanghai'))
        cutoff = datetime.combine(date.fromisoformat(target),time(15),ZoneInfo('Asia/Shanghai'))
        if saved.tzinfo is None or not start <= saved <= cutoff or saved > datetime.now(timezone.utc):
            raise ValueError('invalid_saved_at')
    except (ValueError,TypeError,AttributeError):
        return {'status':'source_unavailable','reason':'B2 来源不可用：保存时间无效，或来源在目标日收盘后才保存','hit_count':None}
    results = []
    seen = set()
    for item in snapshot['candidates']:
        if not isinstance(item, dict) or not isinstance(item.get('symbol'), str) or item['symbol'] in seen:
            return {'status': 'source_unavailable', 'reason': 'B2 来源不可用：候选记录格式错误或重复', 'hit_count': None}
        seen.add(item['symbol'])
    for item in snapshot["candidates"]:
        symbol = item["symbol"]
        try:
            if strict_next_day and (item.get('signal_date') or previous_market_day) != previous_market_day:
                raise ValueError('signal_date_not_previous_market_day')
            value = b2_component(rows_by_symbol.get(symbol, []), target, item.get("signal_date") or previous_market_day)
        except (ValueError, KeyError) as e:
            value = {"status": "not_evaluated", "reasons": [str(e)]}
        results.append({"symbol": symbol, **value})
    return {"status": "completed", "results": results, "hit_count": sum(r["status"] == "hit" for r in results)}
