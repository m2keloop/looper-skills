# Looper Skills

个人实用 AI Agent Skills 集合，持续添加和完善。

## Skills

| Skill | 简介 |
| --- | --- |
| [a-share-policy-screen](./a-share-policy-screen/SKILL.md) | lan选股策略：默认运行 B1、B1 周线、年线金叉回踩；B1 按异动后缩量形态评分，复用增量缓存生成紧凑量价报告。 |
| [a-share-contest-analysis](./a-share-contest-analysis/SKILL.md) | 采集公开炒股比赛热股榜，生成比赛名单、持仓人数排行、每日变化和近期 K 线分析；支持交易日收盘同步。 |
| [ncm-convert](./ncm-convert/SKILL.md) | 使用 ncmdump 将网易云音乐 `.ncm` 文件转换为 MP3 或 FLAC，支持单文件和文件夹批量转换。 |
| [photo-style-batch](./photo-style-batch/SKILL.md) | 根据提示词批量转换照片，支持递增归档、双画面裁切、匿名输出和元数据清除。 |

### a-share-policy-screen

默认执行 **B1、B1 周线、年线金叉回踩**三种 A 股收盘选股策略；B2 已移除。地量、B1_V2、对数乖离率保留历史公式供明确点名时手动运行，不参与默认日终任务。

日线 B1 v2.1 要求近10根有效日K内出现倍量收涨，随后紧接两根或三根的平均量不超过异动日80%，之后收盘保留异动低点下方3%的容差。保留 J、市值与默认主板过滤；完成后继续观察。逐日深缩量、价格稳定和新鲜程度用于0—100形态分，结果默认按分数排序；分数仅表示形态贴合程度，不代表上涨概率。周 B1 保留原公式。

统一入口 `scripts/run_daily.py` 使用版本化 SQLite 行情缓存，只补缺失日K，复权变化或真正缺少历史时才刷新；同一快照供策略、周线、均线与报告使用。结果按策略 Tab 展示紧凑全量名单和联动量价图。历史结果按原公式与分数保存，公式更新另存版本目录。

使用示例：

> 用 $a-share-policy-screen 跑最近收盘日的默认三策略，展示选股结果。

```bash
python3 a-share-policy-screen/scripts/run_daily.py --root OUTPUT/daily-selection --date latest
# 已有目标日快照时，可完全离线复算
python3 a-share-policy-screen/scripts/run_daily.py --root OUTPUT/daily-selection --date YYYY-MM-DD --offline
```

行情适配自 [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data)，所含适配代码许可证见 [a-stock-data-LICENSE.txt](./a-share-policy-screen/references/a-stock-data-LICENSE.txt)。策略命中仅表示规则成立，不生成交易指令。

### a-share-contest-analysis

默认采集参与人数严格大于 1,000 的公开比赛官方热股榜，输出跨比赛排行、比赛名单、每日复盘和可排序 HTML 报告。默认只读官方热榜；显式启用金额分档时仅采集百万组与千万组，权重为 3/5，两组完整且日期、来源核验通过才计算加权。官方热榜仍是全赛基数，不能把加权榜称为只含这两档成员的持仓榜。

运行只需 Python 3.9+、系统 curl 和上海时区数据库，支持 macOS/Linux（Windows 使用 WSL），不依赖原站前端或 Codex 专属工具。随包提供两期公开聚合样本，可先离线生成报告。可由宿主调度在 A 股交易日 16:30 同步，脚本会在周末及交易所假期跳过。

使用示例：

> 用 $a-share-contest-analysis 分析热门比赛里持仓人数最多的股票，和上个快照比较，并结合最近 K 线复盘。

完整运行、存储和跨 AI 平台使用说明见 [SKILL.md](./a-share-contest-analysis/SKILL.md)。运行数据保存于用户工作区，不提交逐人名单、持仓原始响应或检查点。

[用户解决步骤](./a-share-contest-analysis/references/troubleshooting.md) 区分可自行更新的环境、比赛清单、日历、权重与调度配置，以及需要维护者修复的代码问题。新用户通过实际采集建立本地基线，无需下载完整历史数据库。

### ncm-convert

自动检测系统并选择程序，询问待转换文件或文件夹的路径，然后执行转换并检查结果。默认保留原文件，结果保存到原目录；支持递归处理、指定输出目录、大写 `.NCM` 文件和输出重名检查。

随附程序支持 macOS Apple Silicon、Linux x86-64 和 Windows x64。输出为 MP3 还是 FLAC 取决于源文件内的音频格式。

使用示例：

> 用 $ncm-convert 帮我转换音乐。

程序来自 [taurusxin/ncmdump](https://github.com/taurusxin/ncmdump)，许可证见 [LICENSE.ncmdump.txt](./ncm-convert/bin/LICENSE.ncmdump.txt)。

### photo-style-batch

将一个固定原图文件夹中的照片逐张按提示词转换，并为每轮创建不可覆盖的编号目录。它也可以从上下双画面海报中确定性裁切出指定分区。

敏感照片可使用隐私模式：归档不保存完整提示词、绝对路径、原文件名、风格标签或时间；输出使用匿名编号，裁切默认移除 EXIF、GPS、XMP、IPTC 与缩略图元数据。图片交给生成服务前，Skill 还要求先创建去元数据的临时副本。

使用示例：

> 用 $photo-style-batch 把这个文件夹的照片做成黑白粗蜡笔风，并用隐私模式单独归档。

## 使用方式

每个 Skill 独立放在同名文件夹中，包含 `SKILL.md` 及所需资源。将整个 Skill 文件夹复制到所用 Agent 的技能目录；Codex 可放在 `~/.codex/skills/` 下，然后在对话中调用对应 Skill。

