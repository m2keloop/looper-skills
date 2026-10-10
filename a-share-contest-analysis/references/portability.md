# 跨 AI 平台运行

Skill 是可读说明、Python 命令、配置与公开小样本的组合，不绑定某一 AI 产品。宿主 AI 须能读取本地文件并运行本地命令；仅支持普通网页对话、无法执行代码的平台只能阅读方法，不能自行同步。

## 环境与目录

支持 macOS/Linux；Windows 使用 WSL。脚本的文件锁和子进程中断依赖 POSIX，当前不支持原生 Windows。需要 Python **3.9+**、系统 `curl`、IANA 时区数据库中的 `Asia/Shanghai`，以及对公开数据源的 HTTPS 访问。Python 逻辑只用标准库；若精简系统缺少时区数据库，应安装系统时区数据或 `tzdata`，不能把上海时间替换为机器默认时区。

把以下占位绝对路径替换为实际位置，在宿主的 shell 中设置；脚本始终用绝对 Skill 路径执行，数据外置：

```sh
SKILL_DIR="/absolute/path/to/a-share-contest-analysis"
WORK_DIR="/absolute/path/to/writable/contest-work"
mkdir -p "$WORK_DIR/data"
cp "$SKILL_DIR/data/contest-registry.json" "$WORK_DIR/data/contest-registry.json"
python3 --version
curl --version
python3 -c 'from zoneinfo import ZoneInfo; print(ZoneInfo("Asia/Shanghai"))'
```

复制 registry 是首次初始化步骤，不要在每次定时运行前覆盖已经发现的新比赛。运行文件放 `WORK_DIR`；参考配置和样例可从 Skill 目录只读。权重和日历由脚本按 Skill 所在位置读取，如要修改，使用可写的 Skill 工作副本，再设置 `SKILL_DIR` 指向该副本。不要改动只读安装目录或把产生的快照放入 Skill 仓库。

完整目录结构需要保留 `scripts/`、`data/`、`references/`；同目录脚本互相 import，不能只摘出一个文件。默认采集为热股汇总，命令见 [collection.md](collection.md)。离线样例演示见 [analysis.md](analysis.md)。

离线验证不会访问数据源：

```sh
python3 -m unittest discover -s "$SKILL_DIR/tests" -p 'test_*.py'
```

## 外部调度与交易日

Skill **不会自动安装定时任务**。宿主 AI 的 scheduler、系统计划任务或其他调度器可在周一至周五 **Asia/Shanghai 16:30** 调用统一入口，并显式传绝对 `--registry/--snapshots/--report-root`。需要机器、执行环境和网络届时可用；调度器若只支持服务器时区，应换算并保留上海时区语义。

16:30 是收盘后复盘时间，不是盘中交易时段限制。`sync_hot_contests.py` 在任何采集前核对 `data/a-share-trading-calendar.json`：周末、调休周末和交易所节假日跳过，保留报告；长任务至少每 30 秒检查交易日，跨入休市日时中断子任务、等待保存检查点，不发布部分结果。日历缺失、损坏或年份未知时安全关闭联网采集，退出码 `2`；离线 `--analyze-only` 可正常执行。

包内日历目前含 2026 年，来源为[上交所休市公告](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)。每年先依据交易所官方公告补齐 `years`、休市日期、来源和核实日期，再开启该年调度；不能只凭“周一至周五”推定交易日。

统一入口有输出目录锁；退出码 `3` 表示已有任务，不应另开并发采集。调度可用 `sync-latest.json` 判读结果：休市跳过或没有可观察变化时保持安静，仅在新变化、完整报告完成、失败或需处理时通知。通知渠道由宿主配置，Skill 不自行发送消息。

## 数据迁移与 Git

跨设备迁移需要工作目录中的 registry、完整聚合快照及报告；比较历史时保留 `run_id`、源时间与相同统计口径。公开样例不含完整逐人持仓，但运行时的原始第一页排名、分组检查点和日志可能含账号标识，不应直接公开提交。

仓库保存流程代码、配置、说明和少量历史聚合样例；日常全量 exports、续跑检查点和缓存保存在本地工作目录。发布新样例前检查账号标识、本机绝对路径和任何凭据，且标明真实日期与公开来源。其他 AI 只需读取 `SKILL.md` 并按本机绝对路径执行相同命令，不需要原项目界面或某个产品专属工具。
