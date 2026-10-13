"""Independent implementation of the six supported policies, not the Go service.

Only raw data come from a-stock-data. Formula windows are reset for each policy.
All dates are actual bar dates; invalid/missing input never becomes a zero-hit success.
"""
import math
from datetime import date

POLICIES = ["LOW_VOLUME_STABLE_PRICE", "B1", "B1_WEEKLY", "B1_V2",
            "MA_GOLDEN_CROSS_FIRST_PULLBACK", "LOG_BIAS_STRENGTH_FILTER"]
# Current user-selected scope. Legacy formulas remain available only by explicit ID.
DEFAULT_POLICIES = ["B1", "B1_WEEKLY", "MA_GOLDEN_CROSS_FIRST_PULLBACK"]
WINDOWS = dict(zip(POLICIES, [120, 21, 21, 114, 741, 120]))
POLICY_VERSIONS = {pid: pid.lower() + '_v1.0' for pid in POLICIES}
POLICY_VERSIONS.update(B1='b1_volume_contraction_v2.1', B1_V2='b1_yellow_white_v2.0')
B1_PARAMETERS = {'signal_max_age': 10, 'surge_min_multiple': 2.0,
                 'contraction_min_days': 2, 'contraction_max_days': 3,
                 'contraction_mean_max_fraction': .8, 'deep_contraction_fraction': .6,
                 'support_tolerance': .03, 'timing': 'watch',
                 'support': 'all_post_signal_closes_at_or_above_tolerated_signal_low',
                 'score_version': 'b1_shape_score_v1.1'}


def saved_policy_versions(report):
    """Reports predating explicit versions retain their original formula identity."""
    if 'policy_versions' in report:
        return report['policy_versions']
    legacy = {pid: pid.lower() + '_v1.0' for pid in POLICIES}
    legacy['B1_V2'] = 'b1_yellow_white_v2.0'
    return {p['id']: legacy[p['id']] for p in report['policies']}


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


def b1_volume_structure(bars):
    """Mean contraction qualifies; deeper daily contraction and price retention earn points.

    Scoring ranks chart resemblance only. It is neither a probability nor a hit gate.
    KDJ initialization remains the caller's original 21/114-bar window.
    """
    p = B1_PARAMETERS
    events = []
    clip = lambda x: max(0.0, min(1.0, x))
    for i in range(max(1, len(bars)-1-p['signal_max_age']), len(bars)-2):
        signal = bars[i]
        if signal['volume'] < bars[i-1]['volume']*p['surge_min_multiple'] or signal['close'] <= bars[i-1]['close']:
            continue
        following = bars[i+1:]
        windows = []
        for days in range(p['contraction_min_days'],p['contraction_max_days']+1):
            if len(following)<days:
                continue
            window=following[:days]
            fractions=[day['volume']/signal['volume'] for day in window]
            fraction=math.fsum(fractions)/days
            if math.fsum(day['volume'] for day in window)<=signal['volume']*days*p['contraction_mean_max_fraction']:
                windows.append({'days':days,'window':window,'fractions':fractions,'mean':fraction})
        confirmed = bool(windows)
        min_close = min(day['close'] for day in following)
        floor = signal['low']*(1-p['support_tolerance'])
        held = min_close >= floor
        prev5 = bars[max(0, i-5):i]
        baseline = math.fsum(day['volume'] for day in prev5) / len(prev5)
        event = {'date': signal['date'], 'age_bars': len(bars)-1-i,
                 'volume_multiple': signal['volume']/bars[i-1]['volume'],
                 'volume_vs_prev5': signal['volume']/baseline,
                 'signal_close': signal['close'], 'signal_low': signal['low'],
                 'contraction_days': max((w['days'] for w in windows),default=0),
                 'contraction_dates': [], 'contraction_fractions': [],
                 'first3_volume_fractions': [day['volume']/signal['volume'] for day in following[:3]],
                 'support_floor': floor, 'support_held': held,
                 'strict_support_held': min_close>=signal['low'], 'post_min_close': min_close,
                 'pattern_match': confirmed and held,
                 'reasons': ([] if confirmed else ['immediate_mean_contraction_not_completed']) +
                            ([] if held else ['post_signal_close_broke_tolerated_support'])}
        if event['pattern_match']:
            close_band = (max(day['close'] for day in following)-min_close)/signal['close']
            retained = (min_close-floor)/(signal['close']-floor)
            scored=[]
            for w in windows:
                daily_quality=clip((1-max(w['fractions']))/(1-p['deep_contraction_fraction']))
                parts = {'volume_strength': 10*clip((event['volume_multiple']-1)/2) +
                                            10*clip((event['volume_vs_prev5']-1)/2),
                         'contraction_depth': 30*clip((.8-w['mean'])/.6),
                         'price_stability': 15*clip(retained) + 15*clip(1-close_band/.05),
                         'contraction_continuity': 5*(w['days']-1)*daily_quality,
                         'freshness': 10*clip((10-event['age_bars'])/7)}
                scored.append({**w,'shape_score':round(math.fsum(parts.values()),2),
                               'score_parts':{k:round(v,4) for k,v in parts.items()}})
            chosen=max(scored,key=lambda w:(w['shape_score'],w['days']))
            event.update(contraction_days=chosen['days'],
                         contraction_dates=[day['date'] for day in chosen['window']],
                         contraction_fractions=chosen['fractions'],
                         mean_contraction_fraction=chosen['mean'],
                         deep_contraction=all(f<=p['deep_contraction_fraction'] for f in chosen['fractions']),
                         post_close_band_pct=close_band*100,score_parts=chosen['score_parts'],
                         shape_score=chosen['shape_score'],
                         evaluated_windows=[{'days':w['days'],'mean_fraction':w['mean'],
                                             'shape_score':w['shape_score']} for w in scored])
        events.append(event)
    matches = [e for e in events if e['pattern_match']]
    selected = max(matches, key=lambda e: (e['shape_score'], e['date'])) if matches else None
    return {'version': POLICY_VERSIONS['B1'], 'shape_score': selected['shape_score'] if selected else None,
            'signal_date': selected['date'] if selected else None, 'selected_signal': selected,
            'recent_surges': events, 'recent_surge_count': len(events)}


def run_policy(policy, rows, target, finance=None, boards=None, yellow_filter=False, require_pullback=False, _validated=False, _weeks=None):
    need = 114 if yellow_filter and policy in ("B1", "B1_WEEKLY") else WINDOWS[policy]
    try:
        if policy == 'B1_WEEKLY' and not _validated:
            validate(rows, len(rows) or 1, target)
        data = (_weeks if _weeks is not None else aggregate_iso_weeks(rows)) if policy == 'B1_WEEKLY' else rows
        if _validated and policy != 'B1_WEEKLY':
            if len(data) < need:
                raise ValueError(f'insufficient_bars:{len(data)}<{need}')
            if data[-1]['date'] != target:
                raise ValueError('stale_last_bar:' + data[-1]['date'])
            bars = data[-need:]
        else:
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
            bad = [bars[i]["date"] for i in range(len(bars)-5, len(bars)) if bars[i]["close"] <= bars[i-1]["close"] * .905]
            checks.update(j_below_15=indicator["J"] < 15, no_drop_9_5=not bad)
            common.update(**indicator, large_drop_dates=bad)
            if policy == 'B1':
                structure = b1_volume_structure(bars)
                checks.update(recent_volume_surge=bool(structure['recent_surges']),
                              immediate_contraction_2_3_days=any(e['contraction_days']>=2 for e in structure['recent_surges']),
                              contraction_structure_held=structure['selected_signal'] is not None)
                common.update(**structure)
            else:
                events = [{"date": bars[i]["date"], "volume_multiple": bars[i]["volume"] / bars[i-1]["volume"]}
                          for i in range(len(bars)-20, len(bars)) if surge(bars, i)]
                checks.update(two_surges=len(events) >= 2)
                common.update(surge_count=len(events), surges=events)
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
