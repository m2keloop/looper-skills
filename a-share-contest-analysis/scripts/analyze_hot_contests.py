#!/usr/bin/env python3
"""Offline review of complete official contest hot-list snapshots (standard library)."""
import argparse
import csv
import hashlib
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
WEIGHTS_PATH = ROOT / "data" / "consensus-weights.json"
ACTIVE_GROUP_CODES = ("100", "1000")
WEIGHTING_MODEL_VERSION = "public-large-capital-groups-v2"
DEFAULT_WEIGHTS = {
    "version": WEIGHTING_MODEL_VERSION,
    "weights": {"100": 3, "1000": 5},
    "labels": {"100": "百万组", "1000": "千万组"},
    "formula": "官方热榜持仓人数合计 + Σ((分组权重 - 1) × 该组公开持仓人数)",
    "scope": "仅分档采集百万组、千万组完整名单及公开持仓；基础官方热榜人数仍为全赛总量，仅对两组公开人数追加权重，并非已剔除其他金额组的纯持仓榜。仅匹配本次官方热榜股票；分数为混合来源观察指标，不是精确分组总人数、资金金额或去重人数。",
}
COMPATIBILITY_FIELDS = ("threshold", "scope", "stock_scope", "selection_rule")
NOTES = [
    "统计仅覆盖各比赛官方返回的截断热股榜，不代表全部股票或全部持仓。",
    "各比赛持仓人数之和不能当作去重后的独立投资者人数。",
    "退出热榜不表示持仓人数为零；入榜、退出及人数变化不能直接推断买卖。",
    "排名变化以基线名次减本次名次表示，正数表示上升；比较日期以报告标注为准。",
]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("快照时间必须有时区")
    return result.astimezone(SHANGHAI)


def validate_weights(value):
    if not isinstance(value, dict) or not isinstance(value.get("version"), str) or not value["version"]:
        raise ValueError("共识权重必须有非空版本号")
    weights = value.get("weights")
    if not isinstance(weights, dict) or set(weights) != set(ACTIVE_GROUP_CODES):
        raise ValueError("共识权重只能包含百万组和千万组")
    if any(type(w) not in (int, float) or not math.isfinite(w) or w <= 0 for w in weights.values()):
        raise ValueError("共识权重必须是有限正数")
    if weights["1000"] <= weights["100"]:
        raise ValueError("千万组权重必须严格高于百万组")
    if "active_group_codes" in value and value["active_group_codes"] != list(ACTIVE_GROUP_CODES):
        raise ValueError("活动组别必须为百万组和千万组")
    if "weighting_model_version" in value and value["weighting_model_version"] != WEIGHTING_MODEL_VERSION:
        raise ValueError("当前权重模型版本不一致")
    labels = value.get("labels", {})
    if not isinstance(labels, dict):
        raise ValueError("组别名称必须是对象")
    return {**DEFAULT_WEIGHTS, **value, "weights": dict(weights),
            "labels": {code: labels.get(code, DEFAULT_WEIGHTS["labels"][code])
                       for code in ACTIVE_GROUP_CODES},
            "active_group_codes": list(ACTIVE_GROUP_CODES),
            "weighting_model_version": WEIGHTING_MODEL_VERSION}


def validate_historical_weight_stamp(value):
    """Read an old stamp for raw observations, never as the active score model."""
    if isinstance(value, dict) and set(value.get("weights", {})) == set(ACTIVE_GROUP_CODES):
        return validate_weights(value)
    if not isinstance(value, dict) or not isinstance(value.get("version"), str) or not value["version"]:
        raise ValueError("历史补采权重缺少版本号")
    weights = value.get("weights")
    if not isinstance(weights, dict) or set(weights) != {"1", "10", "100", "1000"}:
        raise ValueError("历史补采权重格式无效")
    if any(type(w) not in (int, float) or not math.isfinite(w) or w <= 0 for w in weights.values()):
        raise ValueError("历史补采权重必须是有限正数")
    legacy_thousand = (value["version"] == "public-thousand-v1"
                       and weights == {"1": 1, "10": 1, "100": 1, "1000": 5})
    if not legacy_thousand and (len(set(weights.values())) != 4 or weights["1000"] != max(weights.values())):
        raise ValueError("历史补采权重格式无效")
    return {"version": value["version"], "weights": dict(weights),
            "active_group_codes": ["1", "10", "100", "1000"],
            "weighting_model_version": "legacy-four-capital-groups"}


def load_weights():
    return validate_weights(read_json(WEIGHTS_PATH) if WEIGHTS_PATH.exists() else DEFAULT_WEIGHTS)


def snapshot_fingerprint(snapshot):
    """Use the collector's byte-level identity, including the official roster."""
    digest = hashlib.sha256()
    for filename in ("manifest.json", "catalog.json", "all-hot-stocks.json"):
        digest.update((snapshot["path"] / filename).read_bytes())
    return digest.hexdigest()


def validate_group_date_identity(meta, snapshot, fingerprint, label):
    """A supplement must observe the same Shanghai date as its immutable base."""
    manifest = snapshot["manifest"]
    base_date = timestamp(manifest["completed_at"]).date().isoformat()
    collection_date = meta.get("collection_date")
    if (not isinstance(collection_date, str)
            or date.fromisoformat(collection_date).isoformat() != collection_date
            or collection_date != base_date):
        raise ValueError(f"{label}采集日期缺失、无效或与基础热榜日期不一致")
    if timestamp(meta["completed_at"]).date().isoformat() != base_date:
        raise ValueError(f"{label}实际完成日期与基础热榜日期不一致")
    if (type(meta.get("schema_version")) is not int or meta["schema_version"] != 1
            or meta.get("base_run_id") != manifest["run_id"]):
        raise ValueError(f"{label}与当前热榜快照身份不一致")
    if "base_completed_at" in meta and meta["base_completed_at"] != manifest["completed_at"]:
        raise ValueError(f"{label}基础快照完成时间不一致")
    if "base_fingerprint" in meta and meta["base_fingerprint"] != fingerprint:
        raise ValueError(f"{label}基础快照指纹不一致")


def load_capital_groups(snapshot, weights, path):
    """Validate both active amount tiers atomically before weighting stocks."""
    labels = weights["labels"]
    state = {"available": False, "collection_complete": False, "records": {}, "completed_at": None,
             "scope": weights["scope"], "source_snapshot_run_id": snapshot["manifest"]["run_id"],
             "weight_stamp": None, "selected_contest_count": len(snapshot["selected"]),
             "completed_contest_count": 0, "member_count": 0, "holdings_success_count": 0,
             "holdings_failed_count": 0, "record_count": 0,
             "expected_record_count": len(snapshot["rows"]), "contests": [], "warnings": [],
             "baseline_eligible": False, "read_complete": False,
             "active_group_codes": list(ACTIVE_GROUP_CODES),
             "weighting_model_version": WEIGHTING_MODEL_VERSION}
    try:
        payload = read_json(path)
        meta, groups = payload["metadata"], payload["groups"]
        if meta.get("schema_version") != 1 or meta.get("base_run_id") != snapshot["manifest"]["run_id"]:
            raise ValueError("分组持仓与当前热榜快照不一致")
        if meta.get("collection_complete") is not True or set(groups) != set(ACTIVE_GROUP_CODES):
            raise ValueError("百万组和千万组公开持仓必须全部采集成功；历史组别不能代替当前两组模型")
        if "active_group_codes" in meta and meta["active_group_codes"] != list(ACTIVE_GROUP_CODES):
            raise ValueError("补采活动组别与当前两组模型不一致")
        if "weighting_model_version" in meta and meta["weighting_model_version"] != WEIGHTING_MODEL_VERSION:
            raise ValueError("补采模型版本与当前两组模型不一致")
        fingerprint = snapshot_fingerprint(snapshot)
        validate_group_date_identity(meta, snapshot, fingerprint, "两档合并数据")
        read_summaries = {code: group["metadata"] for code, group in groups.items()}
        for code, summary in read_summaries.items():
            validate_group_date_identity(summary, snapshot, fingerprint, labels[code])
            if (summary.get("collection_complete") is not True
                    or any(type(summary.get(k)) is not int or summary[k] < 0
                           for k in ("member_count", "holdings_success_count", "holdings_failed_count"))
                    or summary["member_count"] != summary["holdings_success_count"]
                    or summary["holdings_failed_count"] != 0):
                raise ValueError(f"{labels[code]}公开持仓尚未全部读取")
        state.update(collection_complete=True, read_complete=True, completed_at=meta["completed_at"],
                     member_count=sum(m.get("member_count", 0) for m in read_summaries.values()),
                     holdings_success_count=sum(m.get("holdings_success_count", 0) for m in read_summaries.values()),
                     holdings_failed_count=sum(m.get("holdings_failed_count", 0) for m in read_summaries.values()),
                     selected_contest_count=meta.get("selected_contest_count", len(snapshot["selected"])),
                     group_totals={code: {"member_count": m.get("member_count"),
                                          "holdings_success_count": m.get("holdings_success_count"),
                                          "holdings_failed_count": m.get("holdings_failed_count"),
                                          "completed_contest_count": meta.get("completed_contest_count", 0),
                                          "selected_contest_count": meta.get("selected_contest_count", len(snapshot["selected"]))}
                                   for code, m in read_summaries.items()})
        stamp = validate_weights({"version": meta.get("weight_version"), "weights": meta.get("weights")})
        if stamp["version"] != weights["version"] or stamp["weights"] != weights["weights"]:
            raise ValueError("补采权重戳与当前配置不一致")
        base_rows = {(r["contest_key"], r["market"], r["stock_code"]): r for r in snapshot["rows"]}
        combined = {identity: {"group_holder_counts": {}} for identity in base_rows}
        group_totals = {}
        group_contests = {}
        for code, label in labels.items():
            group = groups[code]
            group_meta = group["metadata"]
            if (group_meta.get("group_code") != code or group_meta.get("base_run_id") != meta["base_run_id"]
                    or group_meta.get("collection_complete") is not True):
                raise ValueError(f"{label}公开持仓未完整覆盖当前快照")
            by_key = {}
            for contest in group["contests"]:
                key = contest["contest_key"]
                if key in by_key:
                    raise ValueError(f"{label}比赛名单重复")
                values = [contest.get(k) for k in ("member_count", "holdings_success_count", "holdings_failed_count")]
                if any(type(v) is not int or v < 0 for v in values):
                    raise ValueError(f"{label}成员及持仓读取数非法")
                members, success, failed = values
                if (contest.get("collection_complete") is not True or failed or success != members
                        or contest.get("ranking_complete") is False):
                    raise ValueError(f"{label}存在未读完的比赛成员")
                by_key[key] = contest
            if set(by_key) != snapshot["selected"]:
                raise ValueError(f"{label}未覆盖全部入选比赛")
            group_contests[code] = by_key
            group_totals[code] = {"member_count": group_meta["member_count"],
                                  "holdings_success_count": group_meta["holdings_success_count"],
                                  "holdings_failed_count": group_meta["holdings_failed_count"],
                                  "completed_contest_count": sum(c["collection_complete"] for c in group["contests"]),
                                  "selected_contest_count": len(snapshot["selected"]),
                                  "completed_at": group_meta["completed_at"]}
            seen = set()
            for row in group["records"]:
                identity = (row["contest_key"], str(row.get("market")), row.get("stock_code"))
                if identity not in base_rows or identity in seen:
                    raise ValueError(f"{label}记录包含重复股票或当前热榜外股票")
                members = by_key[identity[0]]["member_count"]
                count = row.get("group_holder_count")
                if type(count) is not int or count < 0 or count > members:
                    raise ValueError(f"{label}公开持仓人数非法")
                combined[identity]["group_holder_counts"][code] = count
                seen.add(identity)
            if seen != set(base_rows):
                raise ValueError(f"{label}记录未逐条覆盖当前官方热榜股票")
        completed_at = max((group_totals[c]["completed_at"] for c in groups), key=timestamp)
        for identity, row in base_rows.items():
            counts = combined[identity]["group_holder_counts"]
            public_sum = sum(counts.values())
            if public_sum > row["holder_count"]:
                raise ValueError("两组公开持仓人数之和高于官方热榜人数；来源或时点口径不一致")
            combined[identity].update(market=identity[1], group_holder_counts=counts,
                                      thousand_holder_count=counts["1000"])
        state.update(available=True, collection_complete=True, read_complete=True, completed_at=completed_at,
                     weight_stamp=stamp, records=combined, group_totals=group_totals,
                     group_contests=group_contests, member_count=sum(v["member_count"] for v in group_totals.values()),
                     holdings_success_count=sum(v["holdings_success_count"] for v in group_totals.values()),
                     holdings_failed_count=sum(v["holdings_failed_count"] for v in group_totals.values()),
                     completed_contest_count=min(v["completed_contest_count"] for v in group_totals.values()),
                     record_count=len(combined), contests=groups["1000"]["contests"],
                     baseline_eligible=True)
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        state["warnings"].append(f"多金额分组加权不可用：{error}。保留官方热榜及比赛覆盖排序，加权分数留空。")
    return state


def load_group_collection(snapshot, weights, hot_only=False):
    """Validate the supplement independently; an unusable supplement preserves base data."""
    state = {"enabled": not hot_only, "available": False, "collection_complete": False, "records": {}, "completed_at": None,
             "scope": "官方热股榜聚合人数；本次不采集逐人持仓。" if hot_only else weights["scope"],
             "source_snapshot_run_id": snapshot["manifest"]["run_id"], "weight_stamp": None,
             "selected_contest_count": len(snapshot["selected"]), "completed_contest_count": 0,
             "member_count": 0, "holdings_success_count": 0, "holdings_failed_count": 0,
             "record_count": 0, "expected_record_count": len(snapshot["rows"]),
             "contests": [], "warnings": [],
             "active_group_codes": list(ACTIVE_GROUP_CODES),
             "weighting_model_version": WEIGHTING_MODEL_VERSION}
    if hot_only:
        return state
    capital_path = snapshot["path"] / "group-holdings.json"
    if capital_path.exists():
        return load_capital_groups(snapshot, weights, capital_path)
    path = snapshot["path"] / "thousand-group.json"
    if not path.exists():
        state["warnings"].append("缺少百万组和千万组完整公开持仓数据，暂按入榜比赛数排序；加权分数不可计算。")
        return state
    try:
        payload = read_json(path)
        meta, contests, records = payload["metadata"], payload["contests"], payload["records"]
        state.update(completed_at=meta.get("completed_at"), scope=meta.get("scope") or weights["scope"],
                     contests=contests, record_count=len(records))
        if isinstance(contests, list):
            reported = {c.get("contest_key"): c for c in contests if isinstance(c, dict)
                        and c.get("contest_key") in snapshot["selected"]}
            for field in ("member_count", "holdings_success_count", "holdings_failed_count"):
                state[field] = sum(c[field] for c in reported.values()
                                   if type(c.get(field)) is int and c[field] >= 0)
            state["completed_contest_count"] = sum(
                c.get("collection_complete") is True and c.get("holdings_failed_count") == 0
                and type(c.get("member_count")) is int and c.get("member_count") >= 0
                and c.get("holdings_success_count") == c.get("member_count") for c in reported.values())
        if meta.get("schema_version") != 1 or meta.get("base_run_id") != snapshot["manifest"]["run_id"]:
            raise ValueError("千万组补采与当前热榜快照不一致")
        if str(meta.get("group_code")) != "1000":
            raise ValueError("补采组别不是千万组")
        if meta.get("collection_complete") is not True:
            raise ValueError("千万组公开持仓尚未全部采集成功")
        timestamp(meta["completed_at"])
        if "weight_version" in meta or "weights" in meta:
            state["weight_stamp"] = validate_historical_weight_stamp({
                "version": meta.get("weight_version"), "weights": meta.get("weights")})
        by_key = {}
        for contest in contests:
            key = contest["contest_key"]
            if key in by_key:
                raise ValueError("千万组补采比赛名单重复")
            by_key[key] = contest
            values = [contest.get(k) for k in ("member_count", "holdings_success_count", "holdings_failed_count")]
            if any(type(v) is not int or v < 0 for v in values):
                raise ValueError("千万组成员及请求人数必须是非负整数")
            members, success, failed = values
            complete = contest.get("collection_complete") is True and failed == 0 and success == members
            if not complete:
                raise ValueError("千万组存在未成功读取公开持仓的成员")
        if set(by_key) != snapshot["selected"]:
            raise ValueError("千万组补采未完整覆盖当前入选比赛")
        base_rows = {(r["contest_key"], r["market"], r["stock_code"]): r for r in snapshot["rows"]}
        supplement = {}
        for row in records:
            if not isinstance(row.get("stock_code"), str) or not row["stock_code"] or row.get("market") is None:
                raise ValueError("千万组股票必须保留代码及市场标识")
            identity = (row["contest_key"], str(row["market"]), row["stock_code"])
            if identity not in base_rows or identity in supplement:
                raise ValueError("千万组记录包含重复股票或当前热榜外股票")
            count = row.get("thousand_holder_count")
            if type(count) is not int or count < 0 or count > by_key[identity[0]]["member_count"]:
                raise ValueError("千万组持仓人数非法或超过组内成员人数")
            if count > base_rows[identity]["holder_count"]:
                raise ValueError("千万组公开持仓人数超过官方热榜人数，来源或时点口径不一致；本次关闭加权主榜")
            supplement[identity] = {**row, "market": identity[1], "thousand_holder_count": count,
                                    "group_holder_counts": {"1000": count}}
        if set(supplement) != set(base_rows):
            raise ValueError("千万组记录未逐条覆盖当前官方热榜股票")
        state.update(thousand_group_complete=True, records=supplement, baseline_eligible=True)
        state["warnings"].append("历史快照只采集了千万组，缺少百万组，完整加权分不可计算；该快照仍可比较官方人数。")
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        state["warnings"].append(f"千万组加权不可用：{error}。保留官方热榜及比赛覆盖排序，加权分数留空。")
    return state


def read_snapshot(path):
    path = Path(path).resolve()
    manifest = read_json(path / "manifest.json")
    if manifest.get("collection_complete") is not True:
        raise ValueError(f"拒绝不完整快照：{path.name}")
    if any(manifest.get(k, 0) for k in ("failed_contest_count", "pending_contest_count")):
        raise ValueError("快照存在失败或待采集比赛")
    catalog = read_json(path / "catalog.json")
    payload = read_json(path / "all-hot-stocks.json")
    if payload.get("metadata", {}).get("collection_complete") is not True:
        raise ValueError("合并数据未标记为完整")
    for key in ("run_id", "completed_at", *COMPATIBILITY_FIELDS):
        if manifest.get(key) != payload["metadata"].get(key):
            raise ValueError(f"合并数据与 manifest 不一致：{key}")
    if any(catalog.get(k) != manifest.get(k) for k in ("scope", "threshold")):
        raise ValueError("比赛名单与 manifest 不一致")
    completed = timestamp(manifest["completed_at"])
    contests = catalog["items"]
    by_key = {c["contest_key"]: c for c in contests}
    if len(by_key) != len(contests):
        raise ValueError("比赛名单中存在重复 key")
    selected = set()
    for contest in contests:
        qualifies = contest["participant_count"] > manifest["threshold"]
        if contest.get("qualifies") is not qualifies:
            raise ValueError("比赛入选状态不符合人数阈值")
        if qualifies:
            if contest.get("collection_status") != "collected":
                raise ValueError("入选比赛未完成采集")
            selected.add(contest["contest_key"])
    rows, identities = payload["records"], set()
    for row in rows:
        if row["contest_key"] not in selected:
            raise ValueError("热股数据包含未入选比赛")
        if not isinstance(row["stock_code"], str) or not row["stock_code"]:
            raise ValueError("股票代码必须为非空字符串，保留前导零")
        if row.get("market") is None:
            raise ValueError("股票缺少市场标识")
        row["market"] = str(row["market"])
        identity = (row["contest_key"], row["market"], row["stock_code"])
        if identity in identities:
            raise ValueError("同一比赛存在重复股票")
        identities.add(identity)
        if type(row["holder_count"]) is not int or row["holder_count"] < 0:
            raise ValueError("持仓人数必须为非负整数")
        if type(row["stock_rank"]) is not int or row["stock_rank"] < 1:
            raise ValueError("热股名次必须为正整数")
    for contest in contests:
        if contest["contest_key"] in selected:
            actual = sum(r["contest_key"] == contest["contest_key"] for r in rows)
            if actual != contest.get("returned_stock_count"):
                raise ValueError("比赛返回股票条数与合并数据不一致")
    for key, actual in (("hot_contest_count", len(selected)),
                        ("collected_contest_count", len(selected)),
                        ("stock_row_count", len(rows)),
                        ("catalog_entry_count", len(contests))):
        if manifest.get(key) != actual:
            raise ValueError(f"快照统计不一致：{key}")
    return {"path": path, "manifest": manifest, "completed": completed,
            "date": completed.date().isoformat(), "contests": contests,
            "selected": selected, "rows": rows}


def choose_baseline(root, current, report_date, hot_only=False):
    weights = load_weights()
    current_weighted = load_group_collection(current, weights, hot_only)["available"]
    candidates, weighted_candidates = [], []
    for path in Path(root).glob("*/manifest.json"):
        try:
            snapshot = read_snapshot(path.parent)
        except (ValueError, KeyError, TypeError, OSError):
            continue
        if (snapshot["date"] < min(report_date, current["date"])
                and snapshot["completed"] < current["completed"]
                and all(snapshot["manifest"].get(k) == current["manifest"].get(k)
                        for k in COMPATIBILITY_FIELDS)):
            has_supplement = any((snapshot["path"] / filename).exists()
                                 for filename in ("thousand-group.json", "group-holdings.json"))
            group = load_group_collection(snapshot, weights, hot_only) if has_supplement else None
            # The official base remains comparable even after a supplement fails
            # or the amount-tier model changes. Prefer a compatible weighted base
            # only when the current report also has valid weights.
            candidates.append(snapshot)
            if group and group["available"]:
                weighted_candidates.append(snapshot)
    preferred = weighted_candidates if current_weighted and weighted_candidates else candidates
    return max(preferred, key=lambda s: (s["completed"], s["manifest"]["run_id"]),
               default=None)


def aggregate(rows, group=None, weights=None):
    weighted = group is not None and group["available"]
    weights = weights or DEFAULT_WEIGHTS
    stocks = {}
    for row in rows:
        identity = (row["market"], row["stock_code"])
        entry = stocks.setdefault(identity, {
            "market": identity[0], "stock_code": identity[1], "stock_name": row["stock_name"],
            "contest_count": 0, "holder_count_sum": 0, "contests": [],
            "weighted_consensus_score": 0 if weighted else None,
            "group_holder_counts": {code: 0 for code in weights["weights"]} if weighted else {},
            "thousand_holder_count": 0 if weighted else None, "group_weighting_available": weighted})
        entry["contest_count"] += 1
        entry["holder_count_sum"] += row["holder_count"]
        detail = {k: row.get(k) for k in (
            "contest_key", "competition_name", "holder_count", "stock_rank", "source_updated_at")}
        group_counts = group["records"][(row["contest_key"], *identity)].get("group_holder_counts", {}) if weighted else {}
        thousand = group_counts.get("1000") if weighted else None
        score = row["holder_count"] + sum((weights["weights"][code] - 1) * group_counts.get(code, 0)
                                           for code in weights["weights"]) if weighted else None
        detail.update(weighted_consensus_score=score, group_holder_counts=group_counts,
                      thousand_holder_count=thousand, group_weighting_available=weighted,
                      group_collected_at=group["completed_at"] if group else None)
        entry["contests"].append(detail)
        if weighted:
            entry["weighted_consensus_score"] += score
            for code in weights["weights"]:
                entry["group_holder_counts"][code] += group_counts.get(code, 0)
            entry["thousand_holder_count"] += thousand or 0
    ranking = sorted(stocks.values(), key=lambda r: (
        *([-r["weighted_consensus_score"]] if weighted else []),
        -r["contest_count"], -r["holder_count_sum"], r["stock_code"], r["market"]))
    for rank, entry in enumerate(ranking, 1):
        entry["rank"] = rank
        entry["contests"].sort(key=lambda r: r["contest_key"])
    return ranking


def build_analysis(current, baseline=None, report_date=None, hot_only=False):
    report_date = report_date or current["date"]
    date.fromisoformat(report_date)
    common = sorted(current["selected"] & baseline["selected"]) if baseline else []
    added = sorted(current["selected"] - baseline["selected"]) if baseline else []
    removed = sorted(baseline["selected"] - current["selected"]) if baseline else []
    same_sample = baseline is not None and not added and not removed
    status = "first_snapshot" if baseline is None else (
        "same_contests" if same_sample else "changed_contests")
    contests = [{**c, "failure": c.get("error") or c.get("failure"),
                 "source_url": "https://tzzb.10jqka.com.cn/tzzbWeb/community/"
                               f"hotStockPageWeb.html?key={c['contest_key']}"}
                for c in current["contests"]]
    current_catalog = {c["contest_key"]: c for c in contests}
    previous_catalog = {c["contest_key"]: c for c in baseline["contests"]} if baseline else {}
    weights = load_weights()
    group = load_group_collection(current, weights, hot_only)
    previous_group = load_group_collection(baseline, weights, hot_only) if baseline else None
    previous_weights = previous_group["weight_stamp"] or weights if previous_group else weights
    ranking_method = "weighted_consensus" if group["available"] else "contest_coverage"
    previous_method = ("weighted_consensus" if previous_group["available"] else "contest_coverage") if baseline else None
    method_compatible = ranking_method == previous_method
    weight_compatible = bool(previous_group and group["available"] and previous_group["available"]
                             and previous_group["weight_stamp"]
                             and weights["version"] == previous_weights["version"]
                             and weights["weights"] == previous_weights["weights"]
                             and group["active_group_codes"] == previous_group["active_group_codes"]
                             and group["weighting_model_version"] == previous_group["weighting_model_version"])
    rank_comparable = same_sample and method_compatible and (ranking_method == "contest_coverage" or weight_compatible)
    ranking = aggregate(current["rows"], group, weights)
    previous = {(r["market"], r["stock_code"]): r
                for r in aggregate(baseline["rows"], previous_group, previous_weights)} if baseline else {}
    for row in ranking:
        before = previous.get((row["market"], row["stock_code"])) if same_sample else None
        same_coverage = before and {c["contest_key"] for c in before["contests"]} == {
            c["contest_key"] for c in row["contests"]}
        row_rank_comparable = before and rank_comparable and (ranking_method == "contest_coverage" or same_coverage)
        score_comparable = bool(before and same_coverage and same_sample and weight_compatible)
        group_count_comparable = bool(before and same_coverage and same_sample and group["available"]
                                      and previous_group and previous_group["available"])
        prior_counts = before.get("group_holder_counts", {}) if before else {}
        current_counts = row.get("group_holder_counts", {})
        group_count_deltas = {code: current_counts[code] - prior_counts[code]
                              for code in current_counts.keys() & prior_counts.keys()
                              if group_count_comparable}
        score_delta = row["weighted_consensus_score"] - before["weighted_consensus_score"] if score_comparable else None
        row.update(previous_rank=before["rank"] if row_rank_comparable else None,
                   rank_delta=before["rank"] - row["rank"] if row_rank_comparable else None,
                   previous_contest_count=before["contest_count"] if before else None,
                   contest_count_delta=row["contest_count"] - before["contest_count"] if before else None,
                   previous_holder_count_sum=before["holder_count_sum"] if same_coverage else None,
                   holder_count_sum_delta=row["holder_count_sum"] - before["holder_count_sum"] if same_coverage else None,
                   previous_weighted_consensus_score=before["weighted_consensus_score"] if score_comparable else None,
                   weighted_consensus_score_delta=score_delta, score_delta=score_delta,
                   previous_thousand_holder_count=before["thousand_holder_count"] if group_count_comparable else None,
                   thousand_holder_count_delta=row["thousand_holder_count"] - before["thousand_holder_count"] if group_count_comparable else None,
                   group_holder_counts_delta=group_count_deltas)
    old_rows = {(r["contest_key"], r["market"], r["stock_code"]): r
                for r in baseline["rows"] if r["contest_key"] in common} if baseline else {}
    new_rows = {(r["contest_key"], r["market"], r["stock_code"]): r
                for r in current["rows"] if r["contest_key"] in common}
    changes = []
    for identity in sorted(old_rows.keys() | new_rows.keys()):
        before, now = old_rows.get(identity), new_rows.get(identity)
        name = now or before
        old_record = previous_group["records"].get(identity, {}) if previous_group and previous_group.get("baseline_eligible") else {}
        new_record = group["records"].get(identity, {}) if group.get("baseline_eligible") else {}
        old_counts = old_record.get("group_holder_counts", {})
        new_counts = new_record.get("group_holder_counts", {})
        old_thousand, new_thousand = old_counts.get("1000"), new_counts.get("1000")
        group_comparable = bool(before and now and old_counts and new_counts
                                and group["available"] and previous_group["available"]
                                and previous_group["active_group_codes"] == group["active_group_codes"]
                                and previous_group["weighting_model_version"] == group["weighting_model_version"])
        record_score_comparable = bool(before and now and weight_compatible)
        comparable_codes = old_counts.keys() & new_counts.keys()
        group_count_deltas = {code: new_counts[code] - old_counts[code] for code in comparable_codes} if group_comparable else {}
        weighted_score_delta = (now["holder_count"] - before["holder_count"]
                                + sum((weights["weights"][code] - 1) * group_count_deltas.get(code, 0)
                                      for code in weights["weights"])) if record_score_comparable else None
        group_changed = group_comparable and any(delta != 0 for delta in group_count_deltas.values())
        kind = "new_hot_list" if before is None else "exited_hot_list" if now is None else (
            "changed" if group_changed or any(now[k] != before[k] for k in ("holder_count", "stock_rank")) else "unchanged")
        changes.append({"contest_key": identity[0], "competition_name": name["competition_name"],
                        "market": identity[1], "stock_code": identity[2], "stock_name": name["stock_name"],
                        "change_type": kind, "previous_holder_count": before["holder_count"] if before else None,
                        "holder_count": now["holder_count"] if now else None,
                        "holder_count_delta": now["holder_count"] - before["holder_count"] if now and before else None,
                        "previous_stock_rank": before["stock_rank"] if before else None,
                        "stock_rank": now["stock_rank"] if now else None,
                        "stock_rank_delta": before["stock_rank"] - now["stock_rank"] if now and before else None,
                        "previous_thousand_holder_count": old_thousand,
                        "thousand_holder_count": new_thousand,
                        "thousand_holder_count_delta": new_thousand - old_thousand if group_comparable and old_thousand is not None and new_thousand is not None else None,
                        "previous_group_holder_counts": old_counts,
                        "group_holder_counts": new_counts,
                        "group_holder_counts_delta": group_count_deltas,
                        "weighted_score_delta": weighted_score_delta,
                        "group_count_changed": bool(group_changed)})
    contest_changes = [{"contest_key": k, "competition_name": current_catalog[k]["competition_name"],
                        "previous_participant_count": previous_catalog[k]["participant_count"],
                        "participant_count": current_catalog[k]["participant_count"],
                        "participant_count_delta": current_catalog[k]["participant_count"] - previous_catalog[k]["participant_count"]}
                       for k in common]
    count = lambda kind: sum(r["change_type"] == kind for r in changes)
    hot_changed = count("changed") + count("new_hot_list") + count("exited_hot_list")
    participant_changed = sum(c["participant_count_delta"] != 0 for c in contest_changes)
    changed = hot_changed + participant_changed
    review_status = "first_snapshot" if not baseline else "sample_changed" if not same_sample else (
        "observable_change" if changed
        else "no_observable_change")
    notes = NOTES + (["首次建档：缺少更早日期的完整兼容快照，每日变更不可计算。"] if not baseline else [])
    if baseline and not same_sample:
        notes += ["比赛样本变化：全体排行日差停算，仅比较共同比赛中可观察的热股记录。"]
    if baseline:
        notes += ["持仓人数日差仅在同一比赛、同一股票两日均上榜时计算；合计人数日差还要求该股票覆盖的比赛集合相同。"]
        notes += ["分组公开持仓人数日差按组分别记录；仅两日均完整读取且同一比赛、同一股票都上榜的组才计算，权重变化不改写原始人数变化。"]
    notes += group["warnings"]
    if group["available"]:
        weights_text = "、".join(f"{weights['labels'][code]} {weights['weights'][code]}×"
                                 for code in ACTIVE_GROUP_CODES)
        notes += [weights["scope"], f"各组权重：{weights_text}；"
                  "共识分 = 官方热榜人数 + 各组 Σ((该组权重 − 1) × 该组公开持仓人数)。"]
    if baseline and not method_compatible:
        notes += ["排序方法发生变化：基线与本次采用不同排行口径，综合名次日差停算；原始人数及比赛覆盖变化仍按原口径比较。"]
    elif baseline and ranking_method == "weighted_consensus" and not weight_compatible:
        notes += ["基线分组权重版本/数值不同或缺少采集权重戳，加权分数和综合名次日差停算。"]
    elif baseline and ranking_method == "weighted_consensus":
        notes += ["加权分数及加权主榜名次日差还要求两日百万组和千万组采集完整、模型与权重兼容、比赛样本相同，且该股票覆盖的比赛集合相同。"]
    for contest in contests:
        if not contest["qualifies"]:
            continue
        source_time = contest.get("source_updated_at")
        try:
            source_day = timestamp(source_time).date().isoformat() if source_time else None
        except (ValueError, AttributeError):
            source_day = None
        if source_day is None:
            notes += [f"{contest['competition_name']}：源更新时间缺失或不可解析；采集时间不能当作源更新时间。"]
        elif source_day < report_date:
            notes += [f"{contest['competition_name']}：源更新时间为 {source_time}，早于报告日期；数据可能尚未更新。"]
    manifest = current["manifest"]
    return {"schema_version": 1, "metadata": {
        "report_date": report_date, "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "run_id": manifest["run_id"], "snapshot_path": str(current["path"]),
        "completed_at": manifest["completed_at"], **{k: manifest.get(k) for k in COMPATIBILITY_FIELDS},
        "baseline_run_id": baseline["manifest"]["run_id"] if baseline else None,
        "baseline_completed_at": baseline["manifest"]["completed_at"] if baseline else None,
        "baseline_date": baseline["date"] if baseline else None, "comparison_status": status,
        "ranking_method": ranking_method,
        "active_group_codes": list(ACTIVE_GROUP_CODES),
        "weighting_model_version": WEIGHTING_MODEL_VERSION,
        "collection_mode": "official_hot_lists" if hot_only else "capital_groups",
        "group_weighting": {**weights, "enabled": not hot_only, "collection_complete": group["available"],
                            "completed_at": group["completed_at"], "source_snapshot_run_id": manifest["run_id"]},
        "group_collection": {k: v for k, v in group.items() if k not in ("records", "weight_stamp")},
        "notes": notes},
        "summary": {"catalog_count": len(contests), "selected_contest_count": len(current["selected"]),
                    "stock_row_count": len(current["rows"]), "unique_stock_count": len(ranking),
                    "baseline_available": baseline is not None, "sample_changed": bool(baseline and not same_sample),
                    "common_contest_count": len(common), "added_contest_count": len(added),
                    "removed_contest_count": len(removed), "observed_change_count": changed,
                    "hot_record_change_count": hot_changed, "participant_change_count": participant_changed,
                    "new_hot_list_count": count("new_hot_list"), "exited_hot_list_count": count("exited_hot_list"),
                    "group_weighting_available": group["available"],
                    "group_record_change_count": sum(r["group_count_changed"] for r in changes),
                    "group_holder_change_counts": {code: sum(r.get("group_holder_counts_delta", {}).get(code) not in (None, 0)
                                                              for r in changes) for code in weights["weights"]},
                    "thousand_holder_change_count": sum(r.get("group_holder_counts_delta", {}).get("1000") not in (None, 0)
                                                         for r in changes),
                    "weighted_score_change_count": sum(r["score_delta"] not in (None, 0) for r in ranking),
                    "ranking_method_changed": bool(baseline and not method_compatible),
                    "weight_configuration_comparable": weight_compatible,
                    "review_status": review_status}, "contests": contests, "ranking": ranking,
        "comparison": {"aggregate_comparable": same_sample, "rank_comparable": bool(rank_comparable),
                       "weighted_score_comparable": bool(same_sample and weight_compatible), "common_contest_keys": common,
                       "added_contests": [current_catalog[k] for k in added],
                       "removed_contests": [previous_catalog[k] for k in removed],
                       "contest_changes": contest_changes, "stock_changes": changes}}


def csv_file(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def markdown(analysis):
    meta, summary = analysis["metadata"], analysis["summary"]
    text = [f"# {meta['report_date']} 热股复盘", "", f"采集完成：{meta['completed_at']}。"
            f"核查 {summary['catalog_count']} 场比赛，入选 {summary['selected_contest_count']} 场（参与人数 > {meta['threshold']}），"
            f"共 {summary['stock_row_count']} 条热股记录、{summary['unique_stock_count']} 只股票。", ""]
    if not summary["baseline_available"]:
        text += ["**首次建档：没有更早日期的完整兼容快照，暂不能计算每日变化。**", ""]
    else:
        text += [f"比较基线：{meta['baseline_date']}（{meta['baseline_completed_at']}）。", ""]
        if summary["sample_changed"]:
            text += [f"比赛样本变化：新增 {summary['added_contest_count']} 场、退出 {summary['removed_contest_count']} 场；"
                     f"仅在 {summary['common_contest_count']} 场共同比赛内比较，综合排行日差停算。", ""]
        if summary["hot_record_change_count"] == 0:
            text += ["共同比赛的热股榜无可观察变化；不能据此推断当天交易活动。", ""]
        else:
            text += [f"共同比赛的热股榜有 {summary['hot_record_change_count']} 条可观察变化，"
                     f"其中新上榜 {summary['new_hot_list_count']} 条、退出热榜 {summary['exited_hot_list_count']} 条。", ""]
        text += [f"共同比赛中 {summary['participant_change_count']} 场的参与人数发生变化。", ""]
        if summary["group_record_change_count"]:
            text += [f"共同比赛中 {summary['group_record_change_count']} 条热股记录至少有一档公开持仓人数发生变化；"
                     "同一记录的多档人数变化只计为一条热股变更。", ""]
        for label, key in (("新增入选比赛", "added_contests"), ("退出入选比赛", "removed_contests")):
            if analysis["comparison"][key]:
                text += [label + "：" + "、".join(c["competition_name"] for c in analysis["comparison"][key]) + "。", ""]
    weighted = meta["ranking_method"] == "weighted_consensus"
    hot_only = meta.get("collection_mode") == "official_hot_lists"
    order = "按加权共识分、入榜比赛数、各比赛持仓人数之和排序。" if weighted else "百万组和千万组数据尚不可用，按入榜比赛数、各比赛持仓人数之和排序；加权分数留空。"
    if hot_only:
        order = "本次仅使用官方热股榜，按入榜比赛数、各比赛持仓人数之和排序；不计算金额分档权重。"
        columns = ["| 排名 | 股票 | 代码 | 入榜比赛数 | 各比赛持仓人数之和 | 人数变化 |", "|---:|---|---|---:|---:|---:|"]
    else:
        columns = ["| 排名 | 股票 | 代码 | 加权共识分 | 百万组 | 千万组 | 入榜比赛数 | 各比赛持仓人数之和 | 分数变化 |", "|---:|---|---|---:|---:|---:|---:|---:|---:|"]
    text += ["## 热股排行", "", order, "", *columns]
    for row in analysis["ranking"][:15]:
        if hot_only:
            delta = "—" if row["holder_count_sum_delta"] is None else str(row["holder_count_sum_delta"])
            text += [f"| {row['rank']} | {row['stock_name']} | {row['stock_code']} | {row['contest_count']} | {row['holder_count_sum']} | {delta} |"]
            continue
        score, delta = ("—" if row[k] is None else str(row[k]) for k in ("weighted_consensus_score", "score_delta"))
        counts = row.get("group_holder_counts", {})
        group_cells = [str(counts.get(code, "—")) for code in ACTIVE_GROUP_CODES]
        text += [f"| {row['rank']} | {row['stock_name']} | {row['stock_code']} | {score} | {' | '.join(group_cells)} | {row['contest_count']} | {row['holder_count_sum']} | {delta} |"]
    text += ["", "## 比赛名单", "", "| 比赛 | key | 参与人数 | 入选 | 采集状态 | 源时间 |", "|---|---|---:|---|---|---|"]
    for row in analysis["contests"]:
        text += [f"| {row['competition_name']} | {row['contest_key']} | {row['participant_count']} | "
                 f"{'是' if row['qualifies'] else '否'} | {row['collection_status']} | {row.get('source_updated_at', '—')} |"]
    if analysis["comparison"]["stock_changes"]:
        columns = (["| 比赛 | 股票 | 变更 | 官方人数变化 | 官方名次变化 |", "|---|---|---|---:|---:|"] if hot_only else
                   ["| 比赛 | 股票 | 变更 | 官方人数变化 | 百万组变化 | 千万组变化 | 官方名次变化 | 共识分变化 |", "|---|---|---|---:|---:|---:|---:|---:|"])
        text += ["", "## 每日变更", "", *columns]
        labels = {"changed": "数值变化", "new_hot_list": "新上榜", "exited_hot_list": "退出热榜"}
        for row in analysis["comparison"]["stock_changes"]:
            if row["change_type"] == "unchanged":
                continue
            values = ["—" if row["holder_count_delta"] is None else str(row["holder_count_delta"])]
            if not hot_only:
                values.extend("—" if row.get("group_holder_counts_delta", {}).get(code) is None else str(row["group_holder_counts_delta"][code])
                              for code in ACTIVE_GROUP_CODES)
            values.extend("—" if row[k] is None else str(row[k]) for k in (("stock_rank_delta",) if hot_only else ("stock_rank_delta", "weighted_score_delta")))
            text += [f"| {row['competition_name']} | {row['stock_name']} {row['stock_code']} | "
                     f"{labels[row['change_type']]} | {' | '.join(values)} |"]
    text += ["", "## 统计口径", ""] + [f"- {note}" for note in meta["notes"]]
    return "\n".join(text) + "\n"


def write_report(analysis, root, publish=True):
    root = Path(root).resolve()
    parent = root / analysis["metadata"]["report_date"]
    parent.mkdir(parents=True, exist_ok=True)
    run_id = analysis["metadata"]["run_id"]
    if Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("非法 run_id")
    output, suffix = parent / run_id, 1
    while output.exists():
        output, suffix = parent / f"{run_id}-{suffix}", suffix + 1
    output.mkdir()
    (output / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rank_fields = ["rank", "market", "stock_code", "stock_name", "contest_count", "holder_count_sum",
                   "weighted_consensus_score", "thousand_holder_count", "group_weighting_available",
                   "previous_weighted_consensus_score", "weighted_consensus_score_delta", "score_delta",
                   "previous_thousand_holder_count", "thousand_holder_count_delta",
                   "previous_rank", "rank_delta", "previous_contest_count", "contest_count_delta",
                   "previous_holder_count_sum", "holder_count_sum_delta",
                   *[f"group_holder_count_{code}" for code in ACTIVE_GROUP_CODES]]
    ranked_rows = [{**row, **{f"group_holder_count_{code}": row.get("group_holder_counts", {}).get(code)
                               for code in ACTIVE_GROUP_CODES}}
                   for row in analysis["ranking"]]
    csv_file(output / "hot-stock-ranking.csv", ranked_rows, rank_fields)
    change_fields = ["contest_key", "competition_name", "market", "stock_code", "stock_name", "change_type",
                     "previous_holder_count", "holder_count", "holder_count_delta", "previous_stock_rank", "stock_rank", "stock_rank_delta",
                     "previous_thousand_holder_count", "thousand_holder_count", "thousand_holder_count_delta", "weighted_score_delta", "group_count_changed",
                     *[f"previous_group_holder_count_{code}" for code in ACTIVE_GROUP_CODES],
                     *[f"group_holder_count_{code}" for code in ACTIVE_GROUP_CODES],
                     *[f"group_holder_count_delta_{code}" for code in ACTIVE_GROUP_CODES]]
    change_rows = [{**row,
                    **{f"previous_group_holder_count_{code}": row.get("previous_group_holder_counts", {}).get(code)
                       for code in ACTIVE_GROUP_CODES},
                    **{f"group_holder_count_{code}": row.get("group_holder_counts", {}).get(code)
                       for code in ACTIVE_GROUP_CODES},
                    **{f"group_holder_count_delta_{code}": row.get("group_holder_counts_delta", {}).get(code)
                       for code in ACTIVE_GROUP_CODES}}
                   for row in analysis["comparison"]["stock_changes"]]
    csv_file(output / "changes.csv", change_rows, change_fields)
    csv_file(output / "contests.csv", analysis["contests"], [
        "contest_key", "competition_name", "participant_count", "qualifies", "collection_status",
        "contest_status", "source_updated_at", "participant_count_checked_at", "failure", "source_url"])
    (output / "daily-review.md").write_text(markdown(analysis), encoding="utf-8")
    pointer = {"relative_path": str(output.relative_to(root)),
               **{k: analysis["metadata"][k] for k in ("report_date", "run_id", "generated_at")}}
    if publish:
        temp = root / ".latest.json.tmp"
        temp.write_text(json.dumps(pointer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(root / "latest.json")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, default=ROOT / "exports" / "hot-contests")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--report-root", type=Path, default=ROOT / "exports" / "hot-contest-analysis")
    parser.add_argument("--date", help="报告日期 YYYY-MM-DD，默认快照完成时间的上海日期")
    parser.add_argument("--no-publish", action="store_true", help="生成独立报告但不更新 latest 指针")
    parser.add_argument("--hot-only", action="store_true", help="仅分析官方热股榜；不加载历史分组持仓或计算分组权重")
    args = parser.parse_args(argv)
    try:
        root = args.snapshots.resolve()
        snapshot_path = args.snapshot
        if snapshot_path is None:
            snapshot_path = (root / read_json(root / "latest.json")["relative_path"]).resolve()
            if not snapshot_path.is_relative_to(root):
                raise ValueError("latest 指针超出快照目录")
        current = read_snapshot(snapshot_path)
        report_date = args.date or current["date"]
        date.fromisoformat(report_date)
        baseline = choose_baseline(root, current, report_date, args.hot_only)
        analysis = build_analysis(current, baseline, report_date, args.hot_only)
        output = write_report(analysis, args.report_root, publish=not args.no_publish)
        print(json.dumps({"report_directory": str(output), "path_to_analysis": str(output / "analysis.json"),
                          "summary": analysis["summary"], "review_status": analysis["summary"]["review_status"]},
                         ensure_ascii=False))
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"分析失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
