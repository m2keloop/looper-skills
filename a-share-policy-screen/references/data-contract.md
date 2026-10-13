# 输入、获取与来源

## 离线输入协议（schema_version 1.0）

下面是结构示例，`bars`须提供策略所需的完整真实数据，不能将示例空数组拿去当选股结果。

```json
{
  "schema_version": "1.0",
  "target_date": "YYYY-MM-DD",
  "scope": "subset",
  "universe_source": "用户提供股票池 / 实际API及筛选口径",
  "market_days": ["YYYY-MM-DD"],
  "calendar_source": "交易所日历 / 已验证市场qfq交易日历",
  "sources": {"bars": "供应商及复权", "finance": "总股本来源", "boards": "板块来源"},
  "stocks": [{
    "symbol": "sh600519",
    "name": "股票名称",
    "asset_status": "ACTIVE",
    "adjust": "qfq",
    "bar_source": "供应商、接口及同口径复权证据",
    "bars_as_of": "YYYY-MM-DDT16:00:00+08:00",
    "volume_unit": "lots",
    "volume_unit_source": "供应商单位说明或交叉验证证据",
    "bars": [],
    "finance": {"total_shares": 1000000000, "source": "实际股本来源", "as_of": "披露/采集时间"},
    "boards": {
      "listing_board": "MAIN",
      "source": "分类来源",
      "industries": ["真实行业标签"],
      "concepts": ["真实概念标签"],
      "members": [{"name": "板块名", "change_pct": 0.5, "date": "YYYY-MM-DD"}]
    },
    "quote": {"date": "YYYY-MM-DD", "amount_yi": 1.5, "turnover_pct": 0.8},
    "verified_suspensions": [],
    "suspension_source": "存在停牌豁免时填写公告证据",
    "errors": []
  }]
}
```

- `market_days`涵盖加载K线范围并包括目标日和前一市场交易日；不得将自然日历当交易日历。日K行：`date,open,high,low,close,volume`，升序、无重复。同一股票同一加载序列不混供应商/复权。
- `scope`仅`subset`或`current_active_sh_sz`为已实现口径。`point_in_time`会拒绝，不能借此声称支持严格历史回测。
- `asset_status`确认ACTIVE；代码前缀白名单由脚本验证。记录不足/采集失败的成员也保留在stocks里，用`errors`表示，不能删除后缩小分母。
- `bars_as_of`必须有时区且晚于目标日15:00。只填实际时间，不能把盘中数据时间改为盘后。
- `volume_unit`只接受`lots`（手）或`shares`（股，脚本除100）。单位未知不可当作手。某些接口科创板原值为股，跨标的表格比较前需逐股核实。
- 地量/B1/周B1需要`finance.total_shares`、有效上市板块分类；`listing_board`为`MAIN/CHINEXT/STAR`。兼容已有板块源的`concept_tags`排除已标记的创业板/科创板，不能把空标签当主板证据。
- 行业/概念展示缺失可留空，不影响不依赖它们的公式。东方财富 slist 混有行业/概念/地域/指数，未经分类的标签只作为“板块标签节选”，不要把第一个标签猜成行业或说成热点。
- `quote`与板块`members`涨跌幅仅同目标日展示，不把当前值贴到历史日期。非必要展示数据缺失显示“—”；必要数据缺失列未评估。
- `verified_suspensions`只豁免有公告证据的内部停牌缺口，不能填补日K或豁免目标日过期。

## 数据采集

`fetch_data.py`捆绑了a-stock-data v3.10.0的只读适配函数，保留Apache-2.0许可。改动和原始SHA在`data-adapter-provenance.json`，许可见`a-stock-data-LICENSE.txt`。采集器使用腾讯qfq、腾讯收盘快照、东方财富板块/股本、mootdx股本备用和深交所交易日历。baostock用于当前活跃全池。

默认 baostock 股票池不可用时，统一入口核验沪深交易所当前完整上市名单：上交所分页总数、上市/退市日期，深交所当前 A 股完整下载表；逐股与快照保存实际来源和采集时间。任一交易所来源不可用、分页不完整或成员重复就失败，不能缩成部分市场或沿用旧日期名单冒充当前全池。用`--symbols`才进入用户指定部分池。`--universe path.json`接受由用户或可信接口提供的当前ACTIVE数组，成员至少`symbol/name/asset_status`。不要以样本运行替代全池请求。

已有数据可按此协议转换，不必重新下载。在线运行的历史/最近K接口在重叠日期验证OHLCV一致后才能合并；重叠不一致不自动拼接。采集会保留检查点，未采到的成员仍列在输入中。

## 行情快照复用

- 同一目标交易日、同一股票池只保存一份规范化 `input.json`。当前三个策略、日/周图和全部 MA 都读取其中的同一份 qfq 日 K，不按策略或图表再次请求行情。
- 再次运行前先检查已有快照的 `target_date`、`scope`、`universe_source`、`adjust=qfq`、`bars_as_of`、最后 K 线日期和所需最长窗口。全部匹配才可复用；缓存存在不等于数据有效。
- `fetch_data.py --date YYYY-MM-DD --reuse-input PRIOR/input.json --out RUN/input.json`只复制通过上述校验的同日快照，返回`network_requests: 0`。默认还要求原采集跨度至少1600个自然日；只运行短窗策略时可用`--lookback-days`声明所需跨度。它故意不接受`--date latest`，避免把上一交易日缓存误当最新结果。
- 新交易日只补新数据并重新核验重叠区；需要更长策略窗口时一次性扩展到本次最长窗口。合并后仍形成一份快照供所有策略使用。
- 板块、行业等展示字段不应触发 K 线重取。需要补展示信息时只补缺失字段，并保留原始 K 线和来源。

获取失败没有无限重试；mootdx备用由子进程60秒超时保护，3次失败后不继续探测每只股票。需要更多重试或其他供应商时沿用输入协议，保留数据来源。

成交量单位可用同日成交额和不复权最高/最低价做交叉核验：分别测试“原值为手/股”时推算的成交均价，只有唯一落入价格区间时才作推断并记录证据。若不能唯一确认，标记未评估，不根据代码盲目除100。

## 版本化缓存协议（schema_version 2.0）

统一入口将真实行情导入共享 SQLite，日快照清单包含 target_date、scope、stock_count、snapshot_id、相对于清单的 cache_ref。数据库不可变快照保存完整股票池元数据和各股历史修订引用；清单摘要、池数及日期在读取时核验。

历史修订保存压缩 OHLCV 数组。前缀逐值一致才追加增量；历史订正、复权或供应商口径变化创建新基线，最多20层追加后建立新基线。旧修订仍可复核。快照固定截止日期和元数据版本；不读取未固定的“最新历史”。旧 schema 1.0 仍可导入或直接离线计算。

root/.run.lock 阻止同一缓存根目录的并发写入。每股采集检查点独立提交；失败保留在全池，不把暂停扫描当成成功结果。快照发布后不可用失败重试参数原地修改，需新的 --run-dir。

用户明确只看主板时使用 `--main-board-only`，在拉取行情之前按已确认上市代码分类缩小扫描池；保存为 `scope=subset`，同时记录 `universe_filter=MAIN`。报告分母为实际主板扫描池，不能宣称全沪深评估。发布前调整扫描范围可沿用同一运行目录中的主板检查点和当日报价，不重复取已完成股票的 K 线。

results.json 的 schema 2.0 只存逐股计算和必要元数据，input_ref 相对结果目录指向固定输入，input_sha256 校验内容。报告重建核验摘要后只取候选对应历史，不重新获取行情或计算策略。

当日成交额、换手及报价一次批量获取并核验日期；股本与板块保留真实来源、时效。正常增量不是逐日全池股本刷新，不能把已缓存值声称为当天已核实最新值。
