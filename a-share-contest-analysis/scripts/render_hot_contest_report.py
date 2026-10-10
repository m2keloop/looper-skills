#!/usr/bin/env python3
"""Render an offline report from the hot-contest analysis JSON.

Usage: python3 scripts/render_hot_contest_report.py path/to/analysis.json
No remote assets, libraries, or data requests are used by the output page.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


def safe_json(value: object) -> str:
    """Keep arbitrary source text inside a non-executable JSON script block."""
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>热门比赛每日复盘 · __TITLE_DATE__</title>
<style>
:root{--ink:#202b3d;--muted:#687388;--line:#e4e8ef;--paper:#fff;--bg:#f4f6fa;--accent:#3154c5;--soft:#edf1ff;--up:#148057;--down:#b64c40;--warn:#92591d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}button,input,select{font:inherit}button,select{cursor:pointer}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #a7b7f5;outline-offset:3px}
.shell{max-width:1380px;margin:0 auto;padding:32px 36px 48px}.masthead{display:flex;justify-content:space-between;gap:24px;align-items:flex-start;margin-bottom:24px}.eyebrow{font-size:12px;color:var(--accent);font-weight:650;letter-spacing:.12em;margin-bottom:7px}h1{font-size:30px;line-height:1.35;letter-spacing:-.03em;margin:0 0 8px}h2{font-size:19px;margin:0 0 6px}h3{font-size:15px;margin:0 0 8px}p{margin:0}.sub{color:var(--muted)}.stamp{font-size:12px;min-width:255px;background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:13px 17px}.stamp dl{display:grid;grid-template-columns:66px 1fr;gap:3px 12px;margin:0}.stamp dt{color:var(--muted)}.stamp dd{margin:0;word-break:break-all}.stamp .run{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}.card{border:1px solid var(--line);background:var(--paper);border-radius:11px;padding:17px 20px}.card .label{color:var(--muted);font-size:12px}.card .value{font-size:28px;font-weight:650;letter-spacing:-.04em;line-height:1.5}.card .hint{font-size:11px;color:var(--muted)}.notice{border:1px solid #dbe2fa;border-left:3px solid var(--accent);background:#f7f9ff;padding:12px 16px;border-radius:7px;margin-bottom:17px;font-size:13px}.notice.warn{background:#fffaf1;border-color:#eedcba;border-left-color:#b37831}.notice p+p{margin-top:5px}.notes{color:var(--muted);font-size:12px;margin-bottom:18px}.notes p+p{margin-top:3px}
.panel{background:var(--paper);border:1px solid var(--line);border-radius:12px;overflow:hidden}.navrow{display:flex;justify-content:space-between;gap:20px;align-items:center;padding:0 22px;border-bottom:1px solid var(--line)}.tabs{display:flex;gap:20px}.tab{border:0;border-bottom:3px solid transparent;background:none;color:var(--muted);padding:17px 0 14px;font-weight:600}.tab[aria-selected="true"]{color:var(--accent);border-bottom-color:var(--accent)}.download{display:flex;gap:12px;flex-wrap:wrap;justify-content:flex-end;font-size:12px}.toolbar{display:flex;align-items:center;gap:12px;padding:18px 22px;border-bottom:1px solid var(--line);background:#fcfdff}.control{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}.control input,.control select{border:1px solid #dce1eb;border-radius:6px;background:#fff;color:var(--ink);padding:7px 10px;max-width:310px}.control input{width:220px}.toolbar .reset{border:0;background:none;padding:5px;color:var(--accent);font-size:12px}.view{padding:22px}.view[hidden]{display:none}.viewtop{display:flex;align-items:start;justify-content:space-between;gap:20px;margin-bottom:16px}.annotation{font-size:12px;color:var(--muted)}.smallbadge{display:inline-block;border:1px solid var(--line);padding:3px 9px;border-radius:20px;font-size:11px;white-space:nowrap;color:var(--muted)}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px}table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:13px}thead{background:#f7f8fb}th{text-align:left;font-size:11px;color:var(--muted);font-weight:600;padding:11px 14px;border-bottom:1px solid var(--line)}td{padding:12px 14px;border-bottom:1px solid #edf0f5;vertical-align:top}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:#fafbfe}.num{text-align:right;font-variant-numeric:tabular-nums}.stockname{font-weight:600;white-space:normal;min-width:100px}.code{font-size:11px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.rank{display:inline-block;color:var(--muted);min-width:24px}.rank.top{font-weight:700;color:var(--accent)}.pill{display:inline-block;font-size:11px;border-radius:5px;padding:2px 7px;background:#f0f2f6;color:var(--muted)}.pill.active{background:var(--soft);color:var(--accent)}.pill.bad{background:#fff1ee;color:var(--down)}.up{color:var(--up)}.down{color:var(--down)}.dim{color:var(--muted)}.cont-name{white-space:normal;min-width:210px;max-width:340px;font-weight:500}.failure{font-size:11px;color:var(--down);max-width:260px;white-space:normal;margin-top:4px}.empty{padding:35px 20px;text-align:center;color:var(--muted);font-size:13px}.summary-cell{min-width:75px}details{font-size:12px}summary{cursor:pointer;color:var(--accent);white-space:nowrap}details[open]{min-width:350px}details table{font-size:11px;margin-top:9px;white-space:normal}details th,details td{padding:7px 9px}.detailname{min-width:180px}.bar{width:70px;height:4px;background:#edf0f8;border-radius:2px;margin-top:6px;margin-left:auto}.bar i{display:block;height:4px;background:#8fa4ec;border-radius:2px}.reviewbox{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:18px}.reviewitem{padding:13px 15px;background:#f7f8fb;border-radius:7px}.reviewitem .k{font-size:12px;color:var(--muted)}.reviewitem .v{font-size:23px;font-weight:650;margin-top:3px}.sectiongap{margin-top:24px}.changecontrols{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);margin-bottom:12px}.samplelist{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px}.samplelist>div{padding:12px 15px;background:#faf8f3;border:1px solid #ede4d5;border-radius:6px}.samplelist ul{margin:0;padding-left:18px;font-size:12px;color:var(--muted)}.foot{font-size:11px;color:var(--muted);margin-top:16px}.legend{margin-top:12px;font-size:12px;color:var(--muted)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.nojs{padding:20px;background:#fff5eb;border:1px solid #ebd4b2}
.sort-button{border:0;padding:0;background:none;color:inherit;font:inherit;font-weight:600;white-space:nowrap}.sort-button:hover{color:var(--accent)}.sort-arrow{display:inline-block;margin-left:5px;color:#9ba6bb;font-size:12px}.sort-button[aria-pressed="true"]{color:var(--accent)}.sort-button[aria-pressed="true"] .sort-arrow{color:var(--accent)}.rank-origin{display:block;font-size:10px;color:var(--muted);white-space:nowrap}.weighting-note{margin:0 0 14px;padding:10px 12px;background:#f7f9ff;border-radius:6px;font-size:12px;color:var(--muted)}.weighting-note p+p{margin-top:4px}.score-pending{font-size:11px;color:var(--muted)}.group-breakdown{margin-top:12px}.group-caption{margin-top:10px;color:var(--muted);font-size:11px}
@media(max-width:900px){.shell{padding:22px 16px}.masthead{flex-direction:column;gap:14px}.stamp{width:100%}.cards{gap:9px}.card{padding:12px}.card .value{font-size:23px}.navrow{align-items:flex-start;flex-direction:column;gap:0;padding-bottom:13px}.download{justify-content:flex-start}.toolbar{flex-wrap:wrap}.view{padding:16px}.reviewbox{gap:8px}}
@media(max-width:560px){h1{font-size:25px}.cards{grid-template-columns:repeat(2,1fr)}.toolbar{padding:14px 16px}.control{width:100%}.control input,.control select{width:100%;max-width:none;flex:1}.tabs{gap:18px}.reviewbox{grid-template-columns:1fr}.samplelist{grid-template-columns:1fr}.viewtop{flex-direction:column;gap:8px}.download{gap:14px}}
</style>
</head>
<body>
<main class="shell">
  <header class="masthead">
    <div><div class="eyebrow">比赛热股观察</div><h1>热门比赛每日复盘</h1><p class="sub" id="subtitle"></p></div>
    <div class="stamp"><dl id="stamp"></dl></div>
  </header>
  <div class="cards" id="cards"></div>
  <div class="notice" id="notice"></div>
  <details class="notes"><summary>统计范围与口径</summary><div id="notes"></div></details>
  <section class="panel">
    <div class="navrow">
      <div class="tabs" role="tablist" aria-label="分析视图">
        <button class="tab" role="tab" aria-selected="true" aria-controls="ranking-panel" id="ranking-tab" data-tab="ranking">热股排行</button>
        <button class="tab" role="tab" aria-selected="false" aria-controls="contests-panel" id="contests-tab" data-tab="contests" tabindex="-1">比赛名单</button>
        <button class="tab" role="tab" aria-selected="false" aria-controls="changes-panel" id="changes-tab" data-tab="changes" tabindex="-1">每日变更</button>
      </div>
      <div class="download" aria-label="下载分析数据">
        <a href="__ANALYSIS_FILE__" download>JSON</a><a href="hot-stock-ranking.csv" download>排行 CSV</a><a href="contests.csv" download>名单 CSV</a><a href="changes.csv" download>变更 CSV</a><a href="daily-review.md" download>复盘文档</a>
      </div>
    </div>
    <div class="toolbar">
      <label class="control">股票搜索<input id="search" type="search" placeholder="输入名称或股票代码" autocomplete="off"></label>
      <label class="control">比赛筛选<select id="contest-filter" aria-label="选择比赛"><option value="">全部比赛</option></select></label>
      <label class="control">排行口径<select id="rank-sort" aria-label="选择热股排序口径"><option value="weighted">加权共识分</option><option value="coverage">入榜比赛数优先</option><option value="holders">持仓人数优先</option><option value="official" hidden>官方榜内名次</option><option value="custom" disabled hidden>按表头排序</option></select></label>
      <button class="reset" id="reset" type="button">重置</button>
    </div>
    <section class="view" id="ranking-panel" role="tabpanel" aria-labelledby="ranking-tab">
      <div class="viewtop"><div><h2 id="ranking-title">跨比赛热股排行</h2><p class="annotation" id="ranking-description"></p></div><span class="smallbadge" id="ranking-count"></span></div>
      <div class="weighting-note" id="weighting-note"></div>
      <div class="tablewrap"><table><thead><tr><th id="position-heading">排序名次</th><th data-sort-key="stock" aria-sort="none"><button class="sort-button" type="button" data-sort="stock" aria-pressed="false">股票<span class="sort-arrow" aria-hidden="true">↕</span></button></th><th class="num" data-sort-key="weighted" aria-sort="none"><button class="sort-button" type="button" data-sort="weighted" aria-pressed="false">加权共识分<span class="sort-arrow" aria-hidden="true">↕</span></button></th><th class="num" data-sort-key="coverage" aria-sort="none"><button class="sort-button" type="button" data-sort="coverage" aria-pressed="false">入榜比赛数<span class="sort-arrow" aria-hidden="true">↕</span></button></th><th class="num" data-sort-key="holders" aria-sort="none"><button class="sort-button" type="button" data-sort="holders" aria-pressed="false"><span id="holder-heading">持仓人数合计</span><span class="sort-arrow" aria-hidden="true">↕</span></button></th><th class="num" data-sort-key="rank_delta" aria-sort="none"><button class="sort-button" type="button" data-sort="rank_delta" aria-pressed="false"><span id="rank-delta-heading">主榜名次变化</span><span class="sort-arrow" aria-hidden="true">↕</span></button></th><th class="num" data-sort-key="holder_delta" aria-sort="none"><button class="sort-button" type="button" data-sort="holder_delta" aria-pressed="false">人数变化<span class="sort-arrow" aria-hidden="true">↕</span></button></th><th id="details-heading">比赛 / 分组明细</th></tr></thead><tbody id="ranking-body"></tbody></table><div class="empty" id="ranking-empty" hidden>没有符合筛选条件的股票</div></div>
      <p class="legend">人数合计为各比赛官方持仓人数相加，未跨比赛去重。股票代码与市场一起用于匹配；本报告仅覆盖各比赛官方热门榜。</p>
    </section>
    <section class="view" id="contests-panel" role="tabpanel" aria-labelledby="contests-tab" hidden>
      <div class="viewtop"><div><h2>已核对比赛名单</h2><p class="annotation" id="contest-description"></p></div><span class="smallbadge" id="contest-count"></span></div>
      <div class="tablewrap"><table><thead><tr><th>比赛名称 / 名单 ID</th><th class="num">参与人数</th><th>热门入选</th><th>采集状态</th><th>比赛状态</th><th>来源更新时间</th><th>官方页面</th></tr></thead><tbody id="contest-body"></tbody></table><div class="empty" id="contest-empty" hidden>没有符合比赛筛选的记录</div></div>
      <p class="legend">未入选比赛也保留在名单中。参与人数未知时不判为不热门；“来源更新时间”缺失时显示 —，不以采集时刻代替。</p>
    </section>
    <section class="view" id="changes-panel" role="tabpanel" aria-labelledby="changes-tab" hidden>
      <div class="viewtop"><div><h2 id="review-title">每日复盘变更</h2><p class="annotation" id="review-description"></p></div><span class="smallbadge" id="review-period"></span></div>
      <div class="reviewbox" id="review-cards"></div>
      <div id="sample-change"></div>
      <label class="changecontrols"><input id="show-unchanged" type="checkbox">同时显示未变化记录</label>
      <h3>共同比赛的股票变化</h3>
      <div class="tablewrap"><table><thead><tr><th>比赛</th><th>股票</th><th>变化类型</th><th class="num">基线人数</th><th class="num">本次人数</th><th class="num">人数变化</th><th class="num" data-group-delta>新手组变化</th><th class="num" data-group-delta>十万组变化</th><th class="num" data-group-delta>百万组变化</th><th class="num" data-group-delta>千万组变化</th><th class="num">基线名次</th><th class="num">本次名次</th><th class="num">名次变化</th></tr></thead><tbody id="change-body"></tbody></table><div class="empty" id="change-empty" hidden></div></div>
      <p class="legend" id="changes-legend">入榜 / 出榜表示进入 / 离开官方热门榜，不能解释为买入 / 清仓。榜外人数未知，显示 —，不填成 0。四档公开持仓人数分别显示基线与本次变化，缺少可比基线时显示 —。绿色箭头表示名次上升，红色箭头表示名次下降。</p>
      <h3 class="sectiongap">比赛参与人数变化</h3>
      <div class="tablewrap"><table><thead><tr><th>比赛 / 名单 ID</th><th class="num">基线参与人数</th><th class="num">本次参与人数</th><th class="num">变化</th></tr></thead><tbody id="participant-body"></tbody></table><div class="empty" id="participant-empty" hidden></div></div>
    </section>
  </section>
  <p class="foot">当前快照：<span id="run-id" class="mono"></span> · 本页面可离线打开，搜索与筛选不请求网络。官方链接在打开时访问数据源。</p>
  <noscript><p class="nojs">请开启浏览器 JavaScript 查看交互报告；也可使用上方 JSON、CSV 和复盘文档读取完整分析。</p></noscript>
</main>
<script id="analysis-data" type="application/json">__DATA__</script>
<script>
"use strict";
const data=JSON.parse(document.getElementById("analysis-data").textContent);
const meta=data.metadata||{}, stats=data.summary||{}, comparison=data.comparison||{};
const contests=Array.isArray(data.contests)?data.contests:[], ranking=Array.isArray(data.ranking)?data.ranking:[];
const baseline=Boolean(stats.baseline_available)&&meta.comparison_status!=="first_snapshot";
const comparable=baseline&&comparison.aggregate_comparable===true;
const first=!baseline, byKey=new Map(contests.map(c=>[String(c.contest_key),c]));
const stockChanges=Array.isArray(comparison.stock_changes)?comparison.stock_changes:[];
const selected=document.getElementById("contest-filter"), search=document.getElementById("search"), rankSort=document.getElementById("rank-sort");
const weighting=meta.group_weighting||{}, groupWeights=weighting.weights||{}, groupLabels=weighting.labels||{};
const groupCollection=meta.group_collection||{};
// A completed read can still fail identity or cross-source consistency checks.
const groupReadComplete=groupCollection.read_complete===true||groupCollection.collection_complete===true;
const groupWarnings=Array.isArray(groupCollection.warnings)?groupCollection.warnings.filter(exists):[];
const hotOnly=meta.collection_mode==="official_hot_lists"||groupCollection.enabled===false;
const weightedReady=!hotOnly&&ranking.some(r=>r.group_weighting_available===true&&numeric(r.weighted_consensus_score));
const defaultSortKey=meta.ranking_method==="weighted_consensus"&&weightedReady?"weighted":"coverage";
let sortState={key:defaultSortKey,direction:"desc"}, globalSortState={...sortState};
function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined&&text!==null)node.textContent=String(text);if(cls)node.className=cls;return node;}
function append(parent,...children){children.filter(Boolean).forEach(n=>parent.appendChild(n));return parent;}
function exists(v){return v!==null&&v!==undefined&&v!=="";}
function numeric(v){return exists(v)&&Number.isFinite(Number(v));}
function number(v){return exists(v)&&Number.isFinite(Number(v))?Number(v).toLocaleString("zh-CN"):"—";}
function scalar(v){return exists(v)?(typeof v==="object"?JSON.stringify(v):String(v)):"—";}
function time(v){return exists(v)?String(v).replace("T"," ").replace(/\+08:00$/, "").replace(/\.\d+(?=Z|[+-]\d\d:\d\d|$)/, ""):"—";}
function signed(v){return exists(v)&&Number.isFinite(Number(v))?(Number(v)>0?"+":"")+number(v):"—";}
function deltaCell(v,rank=false){const cell=el("td",undefined,"num");if(!exists(v)||!Number.isFinite(Number(v))){cell.textContent="—";return cell;}const n=Number(v);cell.textContent=rank?(n>0?"↑ "+number(n):n<0?"↓ "+number(-n):"0"):signed(n);if(n!==0)cell.classList.add(n>0?"up":"down");return cell;}
function groupDeltaCell(row,code){const before=(row.previous_group_holder_counts||{})[code],after=(row.group_holder_counts||{})[code],delta=(row.group_holder_counts_delta||{})[code];const cell=deltaCell(delta);cell.title=(groupLabels[code]||"组 "+code)+"公开持仓人数：基线 "+number(before)+" → 本次 "+number(after);return cell;}
function stockCell(row){return append(el("td"),el("div",row.stock_name||"未命名股票","stockname"),el("div",String(row.stock_code||"—"),"code"));}
function contestCell(c){return append(el("td",undefined,"cont-name"),el("div",c.competition_name||c.contest_key||"未命名比赛"),el("div",c.contest_key||"—","code"));}
function badge(text,kind){return el("span",text,"pill"+(kind?" "+kind:""));}
function validURL(value){try{const u=new URL(String(value));return ["https:","http:"].includes(u.protocol)?u.href:null;}catch(_){return null;}}
function sourceLink(c){const url=validURL(c.source_url)||validURL("https://tzzb.10jqka.com.cn/tzzbWeb/community/detailWeb.html?key="+encodeURIComponent(c.contest_key||""));if(!url)return el("span","—");const a=el("a","查看");a.href=url;a.target="_blank";a.rel="noopener noreferrer";return a;}
function term(){return search.value.trim().toLowerCase();}
function matchesStock(row){return !term()||String(row.stock_name||"").toLowerCase().includes(term())||String(row.stock_code||"").toLowerCase().includes(term());}
function matchesContest(row){return !selected.value||String(row.contest_key)===selected.value;}
function text(id,value){document.getElementById(id).textContent=scalar(value);}
function empty(id,condition,message){const n=document.getElementById(id);n.hidden=!condition;if(message)n.textContent=message;}

text("subtitle",String(meta.report_date||"—")+" · 参与人数 > "+number(meta.threshold)+" 的已发现公开比赛");
[["采集完成",time(meta.completed_at)],["比较基线",baseline?time(meta.baseline_completed_at):"首次建档"],["报告生成",time(meta.generated_at)]].forEach(([k,v])=>append(document.getElementById("stamp"),el("dt",k),el("dd",v)));
text("run-id",meta.run_id);
[["热门比赛",stats.selected_contest_count,"已核对 "+number(stats.catalog_count)+" 场比赛"],["不同股票",stats.unique_stock_count,"按股票代码与市场归并"],["热股记录",stats.stock_row_count,"每场官方热门榜的合并明细"],["可观察变更",baseline?stats.observed_change_count:null,baseline?"在共同比赛中比较":"首次建档，尚无比较基线"]].forEach(([label,value,hint])=>append(document.getElementById("cards"),append(el("div",undefined,"card"),el("div",label,"label"),el("div",number(value),"value"),el("div",hint,"hint"))));
const notice=document.getElementById("notice");
if(first){append(notice,el("p","首次建档：本次快照将作为后续复盘的基线。没有历史数据，人数与名次变化均显示 —。"));}
else if(stats.sample_changed||meta.comparison_status==="changed_contests"){notice.classList.add("warn");append(notice,el("p","比赛样本发生变化。只在两次快照共同入选且采集成功的比赛中比较股票变化；跨样本总排名与人数合计的变化不直接比较。"));}
else{append(notice,el("p",stats.review_status==="no_observable_change"?"与基线相比，共同比赛的热门榜未观察到变化。":"与基线比较共同比赛的官方热门榜，详情见“每日变更”。"));}
if(baseline&&meta.baseline_date===meta.report_date){append(notice,el("p","基线与本次快照位于同一天，本次显示同日快照差异，不称为跨日变化。"));}
if(!hotOnly&&groupCollection.available!==true&&(groupReadComplete||groupWarnings.length)){
  notice.classList.add("warn");
  append(notice,el("p",groupReadComplete?"四档公开持仓已读取，但加权口径校验未通过，本次暂停加权排序。":"四档分组公开持仓尚未全部读取，暂不启用加权排序。"));
  groupWarnings.forEach(warning=>append(notice,el("p",scalar(warning))));
}
const notes=document.getElementById("notes");
if(hotOnly){
  text("details-heading","比赛明细");
  document.querySelectorAll("th[data-group-delta]").forEach(th=>{th.hidden=true;});
  text("changes-legend","入榜 / 出榜表示进入 / 离开官方热门榜，不能解释为买入 / 清仓。榜外人数未知，显示 —，不填成 0。绿色箭头表示名次上升，红色箭头表示名次下降。");
}
[meta.scope,meta.stock_scope,...(Array.isArray(meta.notes)?meta.notes:[])].filter(Boolean).forEach(v=>notes.appendChild(el("p",scalar(v))));
const sourced=contests.filter(c=>exists(c.source_updated_at)).map(c=>String(c.source_updated_at)).sort();
if(sourced.length)notes.appendChild(el("p","热股榜来源时间："+time(sourced[0])+(sourced[0]===sourced[sourced.length-1]?"":" 至 "+time(sourced[sourced.length-1]))+"（北京时间）"));
if(!hotOnly){
notes.appendChild(el("p","四档分组采集覆盖："+ranking.filter(r=>weightedScore(r)!==null).length+" / "+ranking.length+" 只热榜股票可计算加权共识分；仅匹配各比赛原热门榜内的股票，榜外股票不参与本排行。"));
notes.appendChild(el("p","分组人数只统计公开可读取的持仓；不推算未公开持仓。比赛间重复成员未去重。"));
if(exists(groupCollection.completed_at))notes.appendChild(el("p",(groupReadComplete?"分组公开持仓读取完成时间：":"分组采集结束时间：")+time(groupCollection.completed_at)+"（北京时间）"));
if(Object.keys(groupCollection.group_totals||{}).length===4&&numeric(groupCollection.member_count))notes.appendChild(el("p","四档合计读取覆盖："+number(groupCollection.completed_contest_count)+" / "+number(groupCollection.selected_contest_count)+" 场比赛均完成；已读 "+number(groupCollection.holdings_success_count)+" / "+number(groupCollection.member_count)+" 条组内成员记录，读取失败 "+number(groupCollection.holdings_failed_count)+" 条。各档分别读取，成员记录未跨比赛去重。"));
Object.entries(groupCollection.group_totals||{}).forEach(([code,totals])=>notes.appendChild(el("p",(groupLabels[code]||("组 "+code))+"（权重 "+number(groupWeights[code])+"×）："+number(totals.member_count)+" 名组内成员，"+number(totals.holdings_success_count)+" 条公开持仓读取成功，覆盖 "+number(totals.completed_contest_count)+" / "+number(totals.selected_contest_count)+" 场。")));
if(weighting.scope)notes.appendChild(el("p",scalar(weighting.scope)));
}
contests.slice().sort((a,b)=>(Number(b.participant_count)||0)-(Number(a.participant_count)||0)).forEach(c=>{const o=el("option",(c.competition_name||c.contest_key)+" · "+number(c.participant_count)+" 人");o.value=String(c.contest_key);selected.appendChild(o);});
text("contest-description","完整列出本次已核对的 "+number(stats.catalog_count)+" 场比赛，严格按官方参与人数 > "+number(meta.threshold)+" 筛选。股票搜索不影响比赛名单。");

function weightedScore(row){return !hotOnly&&row.group_weighting_available===true&&numeric(row.weighted_consensus_score)?Number(row.weighted_consensus_score):null;}
function unavailableScoreLabel(){return groupReadComplete?"口径待核对":"待采集分组";}
function sortValue(row,key){
  if(key==="stock")return String(row.stock_name||row.stock_code||"");
  if(key==="weighted")return weightedScore(row);
  const v={coverage:row.contest_count,holders:row.holder_count_sum,official:row.official_rank,rank_delta:row.can_compare_rank?row.rank_delta:null,holder_delta:row.can_compare?row.holder_count_sum_delta:null}[key];
  return numeric(v)?Number(v):null;
}
function compareRows(a,b){
  const av=sortValue(a,sortState.key), bv=sortValue(b,sortState.key), aMissing=av===null||av==="", bMissing=bv===null||bv==="";
  if(aMissing!==bMissing)return aMissing?1:-1;
  const diff=aMissing?0:typeof av==="string"?av.localeCompare(bv,"zh-CN"):av-bv;
  if(diff)return sortState.direction==="asc"?diff:-diff;
  return (Number(a.official_rank??a.rank)||99999)-(Number(b.official_rank??b.rank)||99999)||String(a.stock_code||"").localeCompare(String(b.stock_code||""),"en")||String(a.market??"").localeCompare(String(b.market??""),"en");
}
function rankingRows(){
  let rows;
  if(!selected.value){rows=ranking.map(r=>({...r,can_compare:comparable,can_compare_rank:comparable}));}
  else{
    rows=[];
    ranking.forEach(r=>{const detail=(r.contests||[]).find(c=>String(c.contest_key)===selected.value);if(!detail)return;
      const change=stockChanges.find(c=>String(c.contest_key)===selected.value&&String(c.market??"")===String(r.market??"")&&String(c.stock_code)===String(r.stock_code));
      const wasCommon=(comparison.common_contest_keys||[]).map(String).includes(selected.value);
      rows.push({...r,contest_count:1,holder_count_sum:detail.holder_count,contests:[detail],official_rank:detail.stock_rank,
        group_holder_counts:detail.group_holder_counts||{},group_weighting_available:detail.group_weighting_available===true,weighted_consensus_score:detail.weighted_consensus_score??null,
        rank_delta:change?change.stock_rank_delta:null,holder_count_sum_delta:change?change.holder_count_delta:null,can_compare:baseline&&wasCommon,can_compare_rank:baseline&&wasCommon});
    });
  }
  // Assign positions across the full current list before applying the search term.
  return rows.sort(compareRows).map((r,index)=>({...r,display_rank:selected.value&&sortState.key==="official"?r.official_rank:index+1})).filter(matchesStock);
}
function appendGroups(parent,counts,title){
  const keys=counts&&typeof counts==="object"?Object.keys(counts).filter(k=>numeric(counts[k])):[];
  if(!keys.length)return false;
  parent.appendChild(el("p",title,"group-caption"));
  const table=el("table",undefined,"group-breakdown"), head=el("thead"), h=el("tr"), body=el("tbody");
  ["分组","公开持仓人数","权重"].forEach(v=>h.appendChild(el("th",v)));head.appendChild(h);
  keys.sort((a,b)=>(Number(groupWeights[b])||0)-(Number(groupWeights[a])||0)||Number(b)-Number(a)).forEach(key=>{
    const tr=el("tr"), label=groupLabels[key]||(key==="other"?"其他组合计":"组 "+key);
    append(tr,el("td",label),el("td",number(counts[key]),"num"),el("td",numeric(groupWeights[key])?number(groupWeights[key])+"×":"—","num"));body.appendChild(tr);
  });append(table,head,body);parent.appendChild(table);return true;
}
function detailsCell(row){const td=el("td",undefined,"summary-cell"), details=el("details"), items=row.contests||[];details.appendChild(el("summary","查看 "+items.length+" 场"));
  const table=el("table"), head=el("thead"), h=el("tr"), body=el("tbody");["比赛","人数","榜内名次","来源时间"].forEach(v=>h.appendChild(el("th",v)));head.appendChild(h);
  items.slice().sort((a,b)=>(Number(b.holder_count)||0)-(Number(a.holder_count)||0)).forEach(c=>{const tr=el("tr");append(tr,el("td",c.competition_name||c.contest_key,"detailname"),el("td",number(c.holder_count),"num"),el("td",number(c.stock_rank),"num"),el("td",time(c.source_updated_at),"dim"));body.appendChild(tr);});append(table,head,body);details.appendChild(table);
  if(!hotOnly){
  const hasGroups=appendGroups(details,row.group_holder_counts,selected.value?"已采集的分组公开持仓人数":"跨比赛分组公开持仓人数合计（未去重）");
  if(!hasGroups)details.appendChild(el("p",groupReadComplete?"四档持仓读取已完成；加权所需口径待核对，暂不显示分组人数。":"分组人数尚未采集；合计人数不能还原各档。","group-caption"));
  else if(row.group_weighting_available!==true)details.appendChild(el("p",groupReadComplete?"四档持仓读取已完成，但口径待核对，暂不计算加权共识分。":"四档公开持仓尚未全部采集，暂不计算加权共识分。","group-caption"));
  }
  td.appendChild(details);return td;
}
function sortControls(){
  const single=Boolean(selected.value);
  [...rankSort.options].forEach(option=>{option.hidden=hotOnly&&option.value==="weighted"|| (option.value==="official"?!single:option.value==="custom"?false:single&&["weighted","coverage"].includes(option.value));if(option.value==="weighted")option.disabled=hotOnly;});
  rankSort.value=["weighted","coverage","holders","official"].includes(sortState.key)?sortState.key:"custom";
  document.querySelectorAll("th[data-sort-key]").forEach(th=>{const active=th.dataset.sortKey===sortState.key, button=th.querySelector("button"), unavailable=single&&["weighted","coverage"].includes(th.dataset.sortKey);
    th.hidden=hotOnly&&th.dataset.sortKey==="weighted";
    th.setAttribute("aria-sort",active?(sortState.direction==="asc"?"ascending":"descending"):"none");button.setAttribute("aria-pressed",String(active));button.disabled=unavailable;
    button.querySelector(".sort-arrow").textContent=active?(sortState.direction==="asc"?"↑":"↓"):"↕";
    button.setAttribute("aria-label",button.textContent.replace(/[↑↓↕]/g,"").trim()+(active?"，当前"+(sortState.direction==="asc"?"升序":"降序")+"，点击切换":"，点击排序"));
  });
}
function renderWeightingNote(){const target=document.getElementById("weighting-note");target.replaceChildren();
  target.hidden=hotOnly;if(hotOnly)return;
  const available=ranking.some(r=>weightedScore(r)!==null);
  const schedule=Object.keys(groupWeights).sort((a,b)=>Number(a)-Number(b)).map(code=>(groupLabels[code]||("组 "+code))+" "+number(groupWeights[code])+"×").join("、");
  if(numeric(groupCollection.member_count)&&(Number(groupCollection.member_count)>0||groupReadComplete))target.appendChild(el("p",groupReadComplete?number(groupCollection.completed_contest_count)+" 场比赛的四档公开持仓均读完，合计 "+number(groupCollection.holdings_success_count)+" 条成员记录。":number(groupCollection.completed_contest_count)+" / "+number(groupCollection.selected_contest_count)+" 场四档数据完成，已读 "+number(groupCollection.holdings_success_count)+" / "+number(groupCollection.member_count)+" 条成员记录。"));
  target.appendChild(el("p","权重："+schedule+"；千万组最高。共识分 = 官方热榜持仓人数合计 + Σ((分组权重 − 1) × 该组公开持仓人数)。"));
  target.appendChild(el("p",available?"这是混合来源的观察指标，不能视为精确分组总人数或资金额。展开明细可查看各档已采集的公开持仓。":groupReadComplete?"四档持仓读取已完成，但加权所需口径待核对；主榜以入榜比赛数排序，加权共识分显示‘口径待核对’。":"四档公开持仓尚未采集完整，主榜以入榜比赛数排序，加权共识分显示‘待采集分组’。"));
}
function renderRanking(){const body=document.getElementById("ranking-body");body.replaceChildren();const rows=rankingRows(), count=Number(stats.selected_contest_count)||1;
  sortControls();renderWeightingNote();
  const orderNames={weighted:"加权共识分",coverage:"入榜比赛数",holders:selected.value?"持仓人数":"持仓人数合计",stock:"股票名称",rank_delta:selected.value?"官方名次变化":"主榜名次变化",holder_delta:"人数变化",official:"官方榜内名次"};
  text("ranking-title",selected.value?(byKey.get(selected.value)?.competition_name||selected.value)+" · 热股排行":"跨比赛共识排行");
  text("ranking-description","当前按 "+orderNames[sortState.key]+(sortState.direction==="asc"?"升序":"降序")+"显示，点击列标题可切换升降序；缺失值始终排在最后。"+(selected.value?"名次变化沿用该比赛官方榜内名次。":"主榜名次变化沿用分析主榜，临时排序不改变复盘口径。"));
  text("position-heading",selected.value&&sortState.key==="official"?"官方名次":"排序名次");text("rank-delta-heading",selected.value?"官方名次变化":"主榜名次变化");
  text("holder-heading",selected.value?"持仓人数":"持仓人数合计");text("ranking-count",rows.length+" 只股票");
  rows.forEach(r=>{const tr=el("tr"), rankcell=el("td"), cnt=el("td",number(r.contest_count),"num");rankcell.appendChild(el("span",number(r.display_rank),"rank"+(Number(r.display_rank)<=3?" top":"")));
    if(selected.value&&sortState.key!=="official")rankcell.appendChild(el("span","官方 "+number(r.official_rank),"rank-origin"));
    else if(!selected.value&&(sortState.key!==defaultSortKey||sortState.direction!=="desc"))rankcell.appendChild(el("span","主榜 "+number(r.rank),"rank-origin"));
    const score=el("td",undefined,"num"), scoreValue=weightedScore(r);if(scoreValue===null)score.appendChild(el("span",unavailableScoreLabel(),"score-pending"));else score.textContent=number(scoreValue);
    const bar=el("div",undefined,"bar"), i=el("i");i.style.width=Math.min(100,(Number(r.contest_count)||0)/(selected.value?1:count)*100)+"%";bar.appendChild(i);cnt.appendChild(bar);
    append(tr,rankcell,stockCell(r),hotOnly?null:score,cnt,el("td",number(r.holder_count_sum),"num"),deltaCell(r.can_compare_rank?r.rank_delta:null,true),deltaCell(r.can_compare?r.holder_count_sum_delta:null),detailsCell(r));body.appendChild(tr);
  });empty("ranking-empty",rows.length===0);
}
function renderContests(){const body=document.getElementById("contest-body");body.replaceChildren();const rows=contests.filter(matchesContest).slice().sort((a,b)=>(Number(b.participant_count)||0)-(Number(a.participant_count)||0));
  rows.forEach(c=>{const tr=el("tr"), q=el("td"), status=el("td"), label={collected:"已采集",excluded:"未入选",failed:"采集失败",pending:"待采集",not_started:"未开始"}[c.collection_status]||scalar(c.collection_status);
    q.appendChild(badge(c.qualifies===true?"热门入选":c.qualifies===false?"未入选":"待核实",c.qualifies===true?"active":""));status.appendChild(badge(label,c.collection_status==="failed"?"bad":""));if(c.failure)status.appendChild(el("div",scalar(c.failure),"failure"));
    const state=Number(c.contest_status)===0&&exists(c.contest_status)?"进行中":Number(c.contest_status)===2?"已结束":scalar(c.contest_status);const link=el("td");link.appendChild(sourceLink(c));
    append(tr,contestCell(c),el("td",number(c.participant_count),"num"),q,status,el("td",state),el("td",time(c.source_updated_at),"dim"),link);body.appendChild(tr);
  });text("contest-count",rows.length+" 场比赛");empty("contest-empty",rows.length===0);
}
function sampleSection(){const target=document.getElementById("sample-change");target.replaceChildren();if(!baseline||!stats.sample_changed)return;const grid=el("div",undefined,"samplelist");
  [["本次新增比赛",comparison.added_contests||[]],["本次退出比赛",comparison.removed_contests||[]]].forEach(([title,list])=>{const box=el("div"), ul=el("ul");box.appendChild(el("h3",title));list.filter(c=>typeof c==="object"?matchesContest(c):!selected.value||String(c)===selected.value).forEach(c=>ul.appendChild(el("li",typeof c==="object"?(c.competition_name||c.contest_key)+"（"+scalar(c.contest_key)+"）":scalar(c))));if(!ul.children.length)ul.appendChild(el("li","无"));box.appendChild(ul);grid.appendChild(box);});target.appendChild(grid);
}
function renderChanges(){const chosenChanges=stockChanges.filter(c=>matchesContest(c)&&matchesStock(c)), visible=chosenChanges.filter(c=>document.getElementById("show-unchanged").checked||c.change_type!=="unchanged");
  text("review-title",baseline&&meta.baseline_date===meta.report_date?"同日快照复盘变更":"每日复盘变更");
  text("review-description",first?"首次建档，等待下一次完整快照后生成比较。":stats.sample_changed?"本次样本变化：仅比较共同比赛；新增或退出比赛单独列出。":"逐比赛比较热门榜股票、持仓人数及榜内名次。");
  text("review-period",baseline?(meta.baseline_date||"基线")+" → "+(meta.report_date||"本次"):"首次建档");
  const cards=document.getElementById("review-cards");cards.replaceChildren();[["共同比赛",baseline?((comparison.common_contest_keys||[]).filter(k=>!selected.value||String(k)===selected.value).length):null],["新入热门榜",baseline?chosenChanges.filter(c=>c.change_type==="new_hot_list").length:null],["退出热门榜",baseline?chosenChanges.filter(c=>c.change_type==="exited_hot_list").length:null]].forEach(([k,v])=>append(cards,append(el("div",undefined,"reviewitem"),el("div",k,"k"),el("div",number(v),"v"))));
  sampleSection();const body=document.getElementById("change-body");body.replaceChildren();
  if(baseline){visible.sort((a,b)=>({new_hot_list:0,exited_hot_list:1,changed:2,unchanged:3}[a.change_type]??4)-({new_hot_list:0,exited_hot_list:1,changed:2,unchanged:3}[b.change_type]??4)||Math.abs(Number(b.holder_count_delta)||0)-Math.abs(Number(a.holder_count_delta)||0)).forEach(c=>{const tr=el("tr"), type=el("td");type.appendChild(badge({changed:"人数 / 名次变化",unchanged:"未变化",new_hot_list:"新入热门榜",exited_hot_list:"退出热门榜"}[c.change_type]||c.change_type,c.change_type==="new_hot_list"?"active":""));
    append(tr,contestCell(c),stockCell(c),type,el("td",number(c.previous_holder_count),"num"),el("td",number(c.holder_count),"num"),deltaCell(c.holder_count_delta));if(!hotOnly)append(tr,groupDeltaCell(c,"1"),groupDeltaCell(c,"10"),groupDeltaCell(c,"100"),groupDeltaCell(c,"1000"));append(tr,el("td",number(c.previous_stock_rank),"num"),el("td",number(c.stock_rank),"num"),deltaCell(c.stock_rank_delta,true));body.appendChild(tr);});}
  empty("change-empty",first||!visible.length,first?"首次建档，暂无比较基线。":term()||selected.value?"当前筛选下没有可显示的变化。":"共同比赛未观察到热门榜变化。可勾选显示未变化记录。");
  document.getElementById("show-unchanged").disabled=first;
  const people=document.getElementById("participant-body");people.replaceChildren();const participants=baseline?(comparison.contest_changes||[]).filter(matchesContest):[];
  participants.forEach(c=>{const tr=el("tr");append(tr,contestCell(c),el("td",number(c.previous_participant_count),"num"),el("td",number(c.participant_count),"num"),deltaCell(c.participant_count_delta));people.appendChild(tr);});empty("participant-empty",!participants.length,first?"首次建档，参与人数变化暂无基线。":"没有可比较的比赛参与人数记录。");
}
function render(){renderRanking();renderContests();renderChanges();}
function activateTab(name){document.querySelectorAll(".tab").forEach(button=>{const active=button.dataset.tab===name;button.setAttribute("aria-selected",String(active));button.tabIndex=active?0:-1;document.getElementById(button.getAttribute("aria-controls")).hidden=!active;});}
document.querySelectorAll(".tab").forEach(button=>{button.addEventListener("click",()=>activateTab(button.dataset.tab));button.addEventListener("keydown",event=>{if(!["ArrowLeft","ArrowRight","Home","End"].includes(event.key))return;event.preventDefault();const tabs=[...document.querySelectorAll(".tab")], i=tabs.indexOf(button);const target=event.key==="Home"?tabs[0]:event.key==="End"?tabs[tabs.length-1]:tabs[(i+(event.key==="ArrowRight"?1:tabs.length-1))%tabs.length];activateTab(target.dataset.tab);target.focus();});});
function chooseSort(key,direction){sortState={key,direction};if(!selected.value)globalSortState={...sortState};renderRanking();}
document.querySelectorAll(".sort-button[data-sort]").forEach(button=>button.addEventListener("click",()=>{const key=button.dataset.sort;chooseSort(key,sortState.key===key?(sortState.direction==="asc"?"desc":"asc"):key==="stock"?"asc":"desc");}));
search.addEventListener("input",render);selected.addEventListener("change",()=>{sortState=selected.value?{key:"official",direction:"asc"}:{...globalSortState};render();});
rankSort.addEventListener("change",()=>chooseSort(rankSort.value,rankSort.value==="official"?"asc":"desc"));document.getElementById("show-unchanged").addEventListener("change",renderChanges);
document.getElementById("reset").addEventListener("click",()=>{search.value="";selected.value="";sortState={key:defaultSortKey,direction:"desc"};globalSortState={...sortState};document.getElementById("show-unchanged").checked=false;render();});
render();
</script>
</body>
</html>
'''


def render_report(analysis_path: Path) -> Path:
    analysis_path = analysis_path.expanduser().resolve()
    data = json.loads(analysis_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("analysis JSON must contain an object")
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("analysis metadata must contain an object")
    report_date = html.escape(str(metadata.get("report_date") or "数据快照"))
    output = analysis_path.parent / "index.html"
    page = PAGE.replace("__TITLE_DATE__", report_date).replace(
        "__ANALYSIS_FILE__", html.escape(analysis_path.name, quote=True)
    ).replace("__DATA__", safe_json(data))
    temp = output.with_suffix(".html.tmp")
    temp.write_text(page, encoding="utf-8")
    temp.replace(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="生成可离线打开的热门比赛分析报告")
    parser.add_argument("analysis_json", type=Path, help="分析 JSON 路径")
    args = parser.parse_args()
    try:
        result = render_report(args.analysis_json)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"报告生成失败：{exc}\n")
    print(json.dumps({"html_path": str(result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
