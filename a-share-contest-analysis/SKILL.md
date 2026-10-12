---
name: a-share-contest-analysis
description: "采集同花顺公开炒股比赛的热门持仓，生成跨比赛热股排行、比赛名单、每日变化与近期 K 线分析。用于比赛持仓数据分析和交易日收盘复盘；不用于交易下单、全市场选股或收益回测。"
---

# A 股比赛热股分析

本 Skill 自带采集、分析、HTML 报告和交易日检查脚本，不依赖原项目、前端依赖或 Codex 专属工具。需要能够读写文件、执行 Python 命令的 AI 环境。运行要求：Python 3.9+、系统 `curl` 和 `Asia/Shanghai` 时区数据库；支持 macOS/Linux，Windows 使用 WSL。

首次使用或更新失败时，先读 [用户可处理事项与解决步骤](references/troubleshooting.md)，优先给出可执行的解决步骤和完成标准。帮助用户配置环境、补录比赛、维护交易日历和安排定时调用；代码缺陷说明维护者责任和当前可用的热榜替代流程。首次采集建立本地基线，下一个交易日再生成变化比较，无需先下载完整历史数据库。

## 默认任务与口径

- 日常默认只采集各比赛官方已汇总的热股榜，比赛筛选为本次官方参与人数 **严格大于 1,000**。发现范围是 buhui 公开目录加本地 registry，不声称覆盖平台所有比赛。
- 官方榜通常只返回每赛约 10 只股票。未入榜不等于无人持有；跨比赛相加的人数没有按人去重。股票以市场加代码匹配，代码保留字符串和前导零。
- 用户关注“持仓人数最多”时，按 `holder_count_sum` 降序查看；`contest_count` 是上榜比赛数。不要把默认比赛覆盖排序当成人数排序。
- 只有用户需要金额分档加权时才执行 `--capital-groups`。这会逐人读取百万组和千万组的完整名单及公开持仓，耗时取决于实际成员数与至少 2 秒的串行请求间隔。两组均完整且核验通过才计算加权，不能推算缺失人数。
- 联网请求串行且至少间隔 2 秒；403/429 立即停止，保留检查点，不换代理或其他途径绕过。失败不覆盖最近完整报告。
- 每日同步按 **A 股交易日北京时间 16:30** 执行。脚本排除周末、调休周末和交易所假期；长采集进入休市日会中断并保留检查点。未知年份先核实交易所公告，补齐 `data/a-share-trading-calendar.json`，不能按普通工作日猜测开市。

详见 [采集流程](references/collection.md)；解释字段、基线和加权前读 [分析口径](references/analysis.md)。

## 在用户工作区运行

`SKILL_DIR` 指本 `SKILL.md` 所在目录，实际执行时填入它的绝对路径。`WORK_DIR` 是用户的可写数据工作目录。保留历史快照，不把运行输出写回安装目录。

首次建立工作目录，把 `data/contest-registry.json` 复制到 `WORK_DIR/data/contest-registry.json`；已有登记表继续复用，避免覆盖用户补充的比赛。示例准备命令仅用于新目录：

```bash
SKILL_DIR=/absolute/path/to/a-share-contest-analysis
WORK_DIR=/absolute/path/to/contest-analysis-work
mkdir -p "$WORK_DIR/data"
cp "$SKILL_DIR/data/contest-registry.json" "$WORK_DIR/data/contest-registry.json"
```

日常采集与复盘：

```bash
python3 "$SKILL_DIR/scripts/sync_hot_contests.py" \
  --hot-only \
  --snapshots "$WORK_DIR/snapshots" \
  --report-root "$WORK_DIR/reports" \
  --registry "$WORK_DIR/data/contest-registry.json"
```

不带模式参数也默认热股榜；显式 `--hot-only` 便于调度和跨平台复用。休市跳过是正常结果，不当作同步成功取得新数据。检查 `sync-latest.json` 中 `complete`、`skipped`、`stage`；发布报告指针与最近任务状态可能属于不同日期。

对已有本地快照离线重算，可在任何日期运行：

```bash
python3 "$SKILL_DIR/scripts/sync_hot_contests.py" \
  --analyze-only --hot-only \
  --snapshots "$WORK_DIR/snapshots" \
  --report-root "$WORK_DIR/reports"
```

两档权重配置为百万/千万 **3/5**，官方组代码为 `100/1000`，读取自 `data/consensus-weights.json`；修改时同时变更版本号。需要时将日常命令中的 `--hot-only` 替换为 `--capital-groups`，完整方法见采集参考。官方热榜人数仍作为全赛基数，只给这两档公开持仓增加权重；不能称为仅两档成员的持仓榜。

两档合并核对基础快照身份和北京时间采集日期；跨日或缺失日期不能生成加权榜。排名失败恢复须重新验证名单终点后才清除旧失败状态。统一入口仍每次新建基础快照，不自动恢复原快照；用户保留检查点，需要恢复时按 [维护者事项](references/troubleshooting.md#应由维护者修复的问题) 提供脱敏诊断，不修改日期、成功状态或失败标记。

## 无网络验证与样本

`assets/sample-snapshots` 包含 2026-10-09、2026-10-10 的真实公开聚合快照，各自只保留三个核心 JSON；没有参赛者账号标识或逐人持仓。它们是历史样本，不是最新行情或完整个人投资组合。

```bash
python3 "$SKILL_DIR/scripts/sync_hot_contests.py" \
  --analyze-only --hot-only \
  --snapshots "$SKILL_DIR/assets/sample-snapshots" \
  --report-root "$WORK_DIR/sample-reports"
```

该命令生成两个样本日期间的变化复盘，JSON/CSV/Markdown/HTML 都保存在工作目录。核对名单、源更新时间、比较基线与样本范围，再向用户汇报排行和变化。样本内容、存储体量和可公开文件见 [数据保存与迁移](references/storage.md)。

## 近期 K 线

需要比较热股走势时，使用已完成报告的 `analysis.json`：

```bash
python3 "$SKILL_DIR/scripts/review_hot_stock_klines.py" \
  --analysis "$WORK_DIR/reports/REPORT_DATE/RUN_ID/analysis.json" \
  --cutoff YYYY-MM-DD \
  --output "$WORK_DIR/reports/REPORT_DATE/RUN_ID/kline-review"
```

脚本读取持仓人数合计前 10 只沪深 A 股，排除基金；腾讯公开日线同时保存前复权与不复权序列。5/20 日涨幅使用前复权价格，涨停收盘形态候选使用不复权单日涨幅和收盘位置；明确它不是官方涨停标签。`VR20` 是最新成交量除以前 20 日成交量中位数，不是盘中量比。所有样本必须具有一致的最新行情日期。

随包 K 线截至 2026-10-09 收盘。生成上述比赛样本报告后，可完全离线生成 K 线复盘：

```bash
python3 "$SKILL_DIR/scripts/review_hot_stock_klines.py" \
  --analysis "$WORK_DIR/sample-reports/2026-10-10/2026-10-10_143434_119930/analysis.json" \
  --candles "$SKILL_DIR/assets/sample-candles.json" \
  --cutoff 2026-10-09 \
  --output "$WORK_DIR/sample-reports/2026-10-10/2026-10-10_143434_119930/kline-review"
```

`--candles` 复用已有行情，不联网；不传该参数才取得新行情。输出 `review.json`、`candles.json` 和 `index.html`，保留样本的原始取得时间；离线不指定截止日期时使用缓存声明的行情日期。缺失所选股票或行情不完整时报错，不能拼接猜测。价格形态只支持描述持仓股票的风格，不能证明参赛者在涨停板买入；持仓榜缺少成交时间、成交价格和持有周期。

## 跨平台与定时执行

整个文件夹可迁移到支持 `SKILL.md` 的 AI 代理；不支持原生 Skill 的环境可直接读取本文并执行相同 Python 命令。调度由宿主 AI 或操作系统提供，Skill 不自动注册任务。外部调度每周一至周五上海时间 16:30 唤醒，脚本再核对交易所日历。安装、运行目录、退出码和年度日历维护见 [平台适配](references/portability.md)。

每日比赛同步和 K 线是两个命令；按排障指南把它们顺序加入宿主调度，并在确认本轮比赛报告完整成功后才更新 K 线。配置步骤可由用户所在 AI 完成；更新后的状态、复盘基线和原始来源时间仍需核对。

离线验证：

```bash
python3 -m unittest discover -s "$SKILL_DIR/tests" -p 'test_*.py'
```
