# 热门比赛采集流程

本 Skill 默认只采集官方汇总热股榜，生成热股排行、比赛名单和每日变化。热门比赛定义为本次官方参与人数 **严格大于 1,000**，不是大于等于；比赛已结束也不自动排除。

发现范围为 [buhui 公开目录](https://buhui.ai/api/v1/competitions) 与本地 `contest-registry.json` 的并集，**不保证覆盖同花顺全部比赛**。目录只用于发现分享 key，人数、名称和股票以同花顺当次响应为准。

## 运行与可写数据目录

先按 [portability.md](portability.md) 设置绝对 `SKILL_DIR` 和可写的 `WORK_DIR`，把 registry 复制到 `WORK_DIR`。所有快照、报告和运行状态保存在工作目录，不写入安装目录：

```sh
python3 "$SKILL_DIR/scripts/sync_hot_contests.py" \
  --hot-only --threshold 1000 --interval 2 \
  --registry "$WORK_DIR/data/contest-registry.json" \
  --snapshots "$WORK_DIR/snapshots" \
  --report-root "$WORK_DIR/reports"
```

`--hot-only` 是默认值；显式传入便于调度和复核。两档模式额外逐人读取百万组与千万组，耗时受实际成员数、分页请求数和串行间隔影响，不应承诺固定几分钟完成。用户可更新环境、名单、日历、权重及调度，处理方法见 [troubleshooting.md](troubleshooting.md)。

## 默认模式的步骤

1. 读取公开目录，合并并去重 registry；成功发现的新 key 写回工作目录中的 registry。
2. 逐比赛请求 `match_ranking_by_share` 第一页，取得名称、赛期、状态及官方 `max_count`。这一页用于查询元数据，不能称为完整选手榜。
3. 以本次 `max_count > threshold` 筛选；不使用目录缓存人数。
4. 对入选比赛读取 `match_stock_ranking_by_share`，保存原始响应、证券代码、市场、名称、官方 `hold_num` 和榜内名次。接口通常返回 10 只，以实际条数为准。
5. 写出独立快照，完整成功后更新快照 `latest.json`；分析和 HTML 均成功后才更新报告入口。

同花顺公开接口根地址为 `https://capital.hexin.cn/caishen_httpserver/direct/caishen_fund/community_share/v1/`；热股分享页面为 `https://tzzb.10jqka.com.cn/tzzbWeb/community/hotStockPageWeb.html?key=<key>`。

请求默认按至少 **2 秒串行间隔**执行。仅用户明确加速时可选用下述百万组并发模式。HTTP **403/429 立即停止**并保存状态，不重试、换代理或切换访问方式绕过限制。其他失败也不能拿历史数据填补并称为本次完整结果。目录不可用时可以保留 registry 中已有查询结果，但该轮不标记为完整成功。

## 两档模式与续跑

需要金额加权时，将日常命令中的 `--hot-only` 替换为 `--capital-groups`，其他工作目录参数保持一致。默认热榜仍不会启动逐人采集。

`--capital-groups` 先读取千万组，再读取百万组；官方 `group_type` 为 `1000` 与 `100`。仅这两组参加分档采集、人数展示与额外加权。每组名单分页到实际空页，校验比赛、组标签、连续排名及重复成员。排名响应中的 `max_count/max_page` 是全比赛口径，不能作为分组人数或分页终点。

之后逐成员读取公开 `position_list`，在同一成员内按代码去重，仅匹配该比赛本次官方热榜的股票。公开列表为空只表示本次没有公开返回股票，不能证明空仓。

用户明确要求加速时，在 `--capital-groups` 同步命令中添加 `--million-workers 2`；只对百万组持仓使用两路并发，每路间隔至少 2 秒，合计约每秒一次请求。官方热榜、名单分页与千万组持仓仍串行。检查点由主线程统一提交，成功记录跳过；任一路 403/429 触发共享停止信号，不派发下一批、不继续重试。已在途请求结束后保存有效结果，缺失数据仍不可加权。

同日原快照的百万组恢复也可将 `--workers 2` 传给 `collect_capital_groups.py`，需保留外层交易日守卫，不应作为绕过日历的日常入口。默认并发为 1；分组元数据 `holding_workers` 记录实际设置，`interval_seconds` 是每路间隔。

采集期间在原快照中保存逐组检查点。正确续跑须复用**同一基础快照、同一采集日期且身份校验通过**的成功读取；不能把昨天成员的持仓直接算作今天已更新。逐组检查点和合并两档同时校验基础快照身份、北京时间采集日期和完成日期；跨日或日期缺失拒绝合并。排名失败恢复仅在重新读取并验证名单终点成功后清除旧失败标记。统一入口重新执行会新建热榜快照，不能称为自动续跑。

需要原快照恢复时，保留原失败快照并提供脱敏错误信息；当前统一入口没有自动续跑选择，日常也可使用 `--hot-only` 获取官方热榜。不要手改状态或把不同快照的检查点拼在一起。直接分组脚本没有统一入口的交易日守卫，也不应作为日常调度入口。向维护者提供脱敏诊断信息的方法见排障指南。

全体入选比赛两组完整读取、人数核对及分析校验通过后才启用加权报告；任一组缺失或失败不推算人数、不生成部分加权排行、不覆盖旧完整报告。完整后保留分组聚合与脱敏日志，清理逐人检查点和逐人原始响应。**正在续跑的检查点含公开账号标识，只留在本地工作目录，不提交 Git。**

## 文件与状态

每个 `snapshots/<run_id>/` 的核心文件：

| 文件 | 内容 |
|---|---|
| `manifest.json` | 参数、范围、完成状态、失败信息和采集时间 |
| `catalog.json` / `contests.csv` | 当次比赛清单、官方人数、入选及采集状态 |
| `all-hot-stocks.json` / `.csv` | 所有入选比赛的官方热股明细 |
| `competitions/<key>/hot-stocks.json` | 单比赛归一化热榜及源时间 |
| `request-log.json`、`raw/` | 请求审计和原始来源响应 |
| `thousand-group.json`、`group-<code>.json` | 可选分组聚合 |
| `group-holdings.json` | 可选两档聚合、基础快照及权重版本 |

证券代码按字符串保存，保留前导零；证券身份按 `market + stock_code` 匹配。`fetched_at` 是采集时间，`source_updated_at` 是官方给出的数据时间，后者缺失时保留缺失，不能用采集时间替代。查看报告同时核对源时间，避免把当日采集误认为当日已更新。

| 退出码 | 含义 |
|---|---|
| `0` | 完整成功，或确认休市而跳过；仍须检查 `complete/skipped` |
| `1` | 参数或程序错误 |
| `2` | 采集不完整或日历无法确认 |
| `3` | 同一输出目录已有采集运行，跳过重叠启动 |

失败快照及状态仍保留；快照和报告的 `latest.json` 各自仅指向对应阶段的完整结果，所以它们不一定属于同一轮。分析口径见 [analysis.md](analysis.md)，交易日和外部调度见 [portability.md](portability.md)。
