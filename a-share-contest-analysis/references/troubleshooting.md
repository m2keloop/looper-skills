# 新用户配置与问题处理

Skill 提供采集和分析方法，不要求下载完整历史数据。用户在自己的工作目录保留核心快照即可；没有历史基线时，先取得第一份完整快照，再比较下一交易日的结果。以下命令先按 [平台适配](portability.md) 设置 `SKILL_DIR`、`WORK_DIR`，均使用用户的可写工作目录。

## 用户可以自己配置或更新

| 情况 | 解决步骤 | 完成标准 |
|---|---|---|
| Python、curl 或时区缺失 | macOS/Linux 安装 Python 3.9+、curl 和系统时区数据；Windows 使用 WSL，在 WSL 内安装并运行。执行下方环境检查。 | `ZoneInfo('Asia/Shanghai')` 可读取；命令运行成功。 |
| 第一次采集没有每日变化 | 在交易日运行 `--hot-only`；保留这次完整快照，下一交易日再采集。 | 报告显示可用的兼容历史基线；不能把随包样本或猜测人数当作自己的昨日数据。 |
| 公开目录没有某场比赛 | 从官方比赛分享 URL 的 `key` 查询参数取得比赛 key，在 `WORK_DIR/data/contest-registry.json` 的 `items` 数组追加 `{"key":"实际key","title":"实际比赛名"}`。保持其他记录与文件结构，勿覆盖已有登记表。 | 下一次采集核实官方人数并按 `--threshold 1000` 筛选，即严格大于 1,000；补录不代表一定入选。 |
| 新年份没有交易日历 | 在可写 Skill 副本中，根据交易所官方年度休市公告补充 `data/a-share-trading-calendar.json`，保留旧年份。JSON 结构见下文。 | 新年份的日期和官方来源均已核实；尚无公告时继续停止联网采集，不能猜测。 |
| 想调整金额权重 | 在可写 Skill 副本的 `data/consensus-weights.json` 修改 `weights`，同时修改 `version`；默认组代码 `1/10/100/1000` 对应权重 `1/2/3/5`。 | 权重为有限正数、各不相同，千万组最高。当前四档有下文代码缺陷，配置修改不能修复它；日常先用 `--hot-only`。 |
| 需要每天自动执行 | 用宿主 AI 或操作系统定时器，在周一至周五 **Asia/Shanghai 16:30** 调用日常采集命令；脚本再核对交易所假期。 | 机器、网络和运行环境届时可用；休市跳过不算取得新数据。 |
| 想同步 K 线 | 比赛同步成功后，再执行 K 线命令。使用发布指针中的实际报告目录，先核对本轮状态；下方提供组合示例。 | `sync-latest.json` 的 `complete=true`，且是本日联网同步；行情日期以 K 线结果为准。 |
| 只想重新分析已有热榜 | 执行下方 `--analyze-only --hot-only` 命令，不联网重新采集。 | 沿用原快照日期和来源时间，不把离线重算称为今日更新。 |
| HTTP 403/429 安全停止 | 立即结束本轮，保留失败状态与检查点，等待来源恢复或按来源方提供的正常访问指引处理。 | 不自动重试，不换代理或切换访问途径规避限制；旧完整报告仍保留。这是保护行为。 |

环境检查：

```sh
python3 --version
curl --version
python3 -c "from zoneinfo import ZoneInfo; print(ZoneInfo('Asia/Shanghai'))"
```

日常采集：

```sh
python3 "$SKILL_DIR/scripts/sync_hot_contests.py" \
  --hot-only --threshold 1000 --interval 2 \
  --registry "$WORK_DIR/data/contest-registry.json" \
  --snapshots "$WORK_DIR/snapshots" --report-root "$WORK_DIR/reports"
```

已有热榜离线重算：

```sh
python3 "$SKILL_DIR/scripts/sync_hot_contests.py" \
  --analyze-only --hot-only \
  --snapshots "$WORK_DIR/snapshots" --report-root "$WORK_DIR/reports"
```

要指定某份旧快照，在上述命令追加 `--snapshot "$WORK_DIR/snapshots/实际RUN_ID"`。只比较完整、口径兼容的快照；不能修改日期让不兼容数据通过核对。

## 年度日历的更新结构

顶层保留 `schema_version: 1`、`timezone: "Asia/Shanghai"`、`years` 等现有字段。在 `years` 中新增年份对象；`holidays` 是该年份全部已核实的休市日期字符串数组，`sources` 是官方公告 URL 数组，`verified_at` 是核实日期，`note` 可记核实范围。示意结构如下，占位符不能直接写入正式配置：

```json
"YYYY": {
  "holidays": ["YYYY-MM-DD"],
  "sources": ["交易所官方年度休市公告URL"],
  "verified_at": "核实日期YYYY-MM-DD",
  "note": "核实依据与范围"
}
```

可由用户或 AI 根据公告逐日展开日期范围，再检查日期均属于对应年份。周六、周日始终休市，调休上班也不视为开市。当前脚本从 Skill 的 `data/` 读取日历和权重，不能只把新配置放进 `WORK_DIR/data/` 后期待生效；只读安装目录应先复制到可写位置，并将 `SKILL_DIR` 指向该副本。

## 比赛同步后再更新 K 线

每日调度让 K 线步骤依赖本轮比赛命令成功退出；下方用 `&&` 连接两步，避免比赛命令在写入状态前失败时沿用旧状态。随后核对本日联网同步状态，并读取报告根目录 `latest.json` 的实际 `relative_path`；休市、失败或离线重算会跳过。旧完整报告存在不代表本轮成功。

```sh
python3 "$SKILL_DIR/scripts/sync_hot_contests.py" \
  --hot-only --threshold 1000 --interval 2 \
  --registry "$WORK_DIR/data/contest-registry.json" \
  --snapshots "$WORK_DIR/snapshots" --report-root "$WORK_DIR/reports" &&
python3 - "$SKILL_DIR" "$WORK_DIR" <<'PY'
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

skill, work = map(Path, sys.argv[1:])
root = work / 'reports'
status_path = root / 'sync-latest.json'
status = json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else {}
today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
if (status.get('complete') is not True or status.get('skipped')
        or status.get('analyze_only') or status.get('trading_date') != today):
    print('没有本日完整联网同步，跳过 K 线更新。')
    raise SystemExit(0)
pointer = json.loads((root / 'latest.json').read_text(encoding='utf-8'))
report = (root / pointer['relative_path']).resolve()
if report != Path(status['report_directory']).resolve():
    raise SystemExit('发布指针与本轮完整报告不一致，停止 K 线更新。')
subprocess.run([
    sys.executable, str(skill / 'scripts/review_hot_stock_klines.py'),
    '--analysis', str(report / 'analysis.json'),
    '--cutoff', pointer['report_date'], '--output', str(report / 'kline-review'),
], check=True)
PY
```

K 线脚本单独运行并请求公开行情。阅读其输出的 `metadata.latest_market_date` 和取得时间；行情尚未更新时展示实际日期，不能标成今日行情。上例不会把 K 线成功与否写回比赛同步状态，调度器需单独记录 K 线步骤的退出码。

## 应由维护者修复的问题

这些问题不是让新用户修改配置就能可靠解决的。当前先使用 **`--hot-only`**；完整四档加权待维护者修复并验证后再启用。

| 已知缺口 | 用户临时处理 | 维护者应完成的修复 |
|---|---|---|
| 四档合并缺少完整跨日校验 | 不发布或信任当前四档加权结果；用官方纯热榜分析。 | 对四档来源日期、基础快照和身份进行一致性校验，并用跨日样本验证拒绝合并。 |
| 排名失败后的 `failed_stage` 残留可能阻止恢复 | 保留原失败快照和检查点，勿手改状态；后续交易日可重新采集独立的纯热榜。 | 修复恢复状态转换，验证中断与恢复后完成状态正确。 |
| 统一入口重新执行会新建快照，没有自动续跑 | 勿把旧检查点拼到新快照，也勿修改日期；需要续跑时等待修复版及其恢复说明。 | 提供明确选择原快照的续跑入口，校验日期和参数，避免重复或混入旧数据。 |
| Linux/WSL 与完整四档流程实测不足 | 环境检查和离线测试可先做，不能据此声称完整联网四档已经验证。 | 在目标环境实测，并完成完整四档样本与失败恢复验证。 |

更新修复版时，保留用户工作目录、补录的 registry 和本地核心快照；重新核对本地日历、权重配置再迁移。无需提供整套历史数据或另建存储系统。

需要反馈给维护者时，提示用户通过 [仓库 Issues](https://github.com/m2keloop/looper-skills/issues) 提供 Skill 提交版本或下载日期、操作系统、Python 版本、退出码、失败阶段和脱敏错误摘要即可；无需上传全部数据、账号标识或整个检查点。使用 Git 下载的用户可在仓库目录执行 `git pull --ff-only` 获取更新，其他安装方式重新下载或使用宿主的 Skill 更新功能。
