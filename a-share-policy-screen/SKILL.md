---
name: a-share-policy-screen
description: "lan选股策略：执行 B1、地量、B1 周线、B1_V2、B2_V1、年线金叉回踩、对数乖离率七种 A 股收盘选股策略，并生成策略 Tab、紧凑股票列表和联动量价图报告。用于按这些规则选股、验证可执行性或复用此报告模板；不用于盘中信号、自动交易或收益回测。"
---

# lan选股策略

将用户提供的、基于 Go 提交 `5ed70450` 的七策略文档独立执行。行情采集适配自 [a-stock-data](https://github.com/simonlin1212/a-stock-data) v3.10.0；不依赖另行安装同名 skill。计算脚本与模板随本 skill 提供。

## 执行约定

- 用户未指定日期时，用上海时间最近已收盘且数据确认到齐的市场交易日。用户说“选股”“运行 lan 选股策略”而未点名策略时，分别运行全部七种；只有用户明确要求“只跑 B1”或明确点名单个策略时才单独运行该策略。不能因旧名称或上次只跑了 B1 而默认只跑 B1。
- 用户指定股票或资产池就使用该范围；否则尝试获取当前活跃沪深 A 股全池。全池不可用就报告失败/覆盖不足，不擅自换成几只熟悉的股票。北交所不在范围内。
- 先读 [策略规则](references/policies.md)。各策略有独立初始化窗口，不能拿图表长历史算出的 J/EMA 替代。黄白线和乖离率回抽开关默认关闭。
- 默认采用保守收盘口径：最后一根 K 必须等于目标日，价格、复权、股本和必要板块分类有来源，缺日 K 不填补。停牌日仅凭已有证据豁免内部缺口；目标日停牌仍不作为当日候选。
- 区分 `hit`、`not_hit`、`not_evaluated`、`source_unavailable`、`failed`。有未评估股票时是部分筛选，不能声称全市场成功零候选。
- B2 只接受前一市场交易日已经保存的指定版本 B1_V2 快照。不能为了补 B2 来源而临时重算前日 B1_V2。额外校验真实保存时间位于前一交易日收盘至目标日收盘之间（保守时间约定，见数据协议）。可选 `--strict-b2-next-day` 更严格要求实际信号日等于上一市场交易日，需标明口径差异。
- 历史日期运行使用最新前复权数据、最新股本及当前活跃池；明确它不是无前视偏差回测。历史回测需另取当时已披露的股本、板块、上市名单，当前脚本不实现回测。

## 路径与数据

下列路径均相对于本 `SKILL.md` 所在目录；在实际命令中使用该目录的绝对路径。所有运行文件写入用户工作区中新建的运行目录，不写入 skill 目录。

已有数据优先复用，但核对日期、复权、覆盖窗口、股票池和来源。输入格式见 [数据协议与获取](references/data-contract.md)。纯计算和报告只需 Python 3 标准库。

同一目标日建立一份规范化行情快照，所有策略共同使用：每只股票的前复权日 K 只采集一次，周 K、指标、MA 和报告走势图都从这份快照在本地生成。不得按策略、Tab、候选股或图表重复拉取同一段 K 线。再次运行同一目标日时，先寻找先前的 `input.json`；日期、股票池、复权、收盘确认和最长策略窗口均满足时直接交给 `screen.py`。缓存窗口不足时只补齐一次缺少的历史，再供全部策略复用。

需要把一份已核验的同日快照复制到新的运行目录时，可使用 `--reuse-input`；该模式要求显式日期并且不访问行情网络：

```bash
python3 scripts/fetch_data.py --date YYYY-MM-DD --reuse-input PRIOR/input.json --out RUN/input.json
```

目标交易日改变、用户改变股票池、缓存并非 qfq、最后 K 线不是目标日或数据窗口不足时，缓存不可冒充新数据。一次运行中即使只展示少数候选，也保留原快照供随后策略复用。

需要联网取数时，使用 `scripts/fetch_data.py`。依赖缺失时，在工作区建立 venv 并安装 `scripts/requirements-data.txt`，不更改系统 Python。公共接口请求遵循限流；一次调用超时/字段变化明确记录，避免无限重试。联网与目录权限按宿主工具处理，不增加额外交易或远端写入权限。

```bash
python3 scripts/fetch_data.py --date latest --out RUN/input.json
# 用户指定的小池（明确标记 subset）
python3 scripts/fetch_data.py --date YYYY-MM-DD --symbols sh600519,sz000001 --out RUN/input.json
```

默认取约 1600 个自然日，兼顾 741 根日 K 与 114 根周 K。联网采集时对每只股票只发起一组历史 K 线请求，不再额外请求一份“图表 K 线”或为每个策略重取；全部策略共享原始数据，但各自截窗初始化指标。收盘行情、板块和股本缺失不拿零值补造。股本采用实际披露数据，不由市值倒推。

## 筛选与保存

```bash
python3 scripts/screen.py --input RUN/input.json --out RUN/result --policies all
python3 scripts/screen.py --input RUN/input.json --out RUN/b1 --policies B1
python3 scripts/screen.py --input RUN/input.json --out RUN/b1-yellow --policies B1 --yellow-filter
python3 scripts/screen.py --input RUN/input.json --out RUN/b2 --policies B2_V1 --b2-snapshot PRIOR/B1_V2.snapshot.json
```

同一次默认选股只执行第一条 `--policies all`；不要为了生成七个 Tab 再运行七次采集。之后若用户只查看或补跑某个策略，仍使用同一个 `RUN/input.json`。

`--require-pullback` 仅影响对数乖离率；`B1_黄线上` 作为打开黄白线过滤的兼容别名。其他非默认阈值目前未做 CLI 参数化；用户要求改阈值时先明确记录，不静默声称默认策略结果。

产物：

- `results.json`：数据来源、选项、逐股计算值、判断与失败原因，供复核。
- `view-model.json`：可复用的展示数据。
- `report.html` / `report.md`：完整独立浏览器版及紧凑 Markdown；体积小于1MB时另生成 `report.inline.html`，见 `render-manifest.json`。
- 成功完整执行 B1_V2 后保存 `B1_V2.snapshot.json`，包含版本、实际信号日与真实保存时间，供未来交易日使用。部分筛选不保存成功来源快照。

同一输出目录不覆盖已完成结果或快照。采集和计算的失败文件不能当成成功报告。查看 `results.json` 统计后再总结，不从截图猜数量。

## 用户确认的报告样式

遵循 [报告模板](references/report-template.md) 和 `assets/report-fragment.html`：**策略之间用 Tab 切换，同一策略的全部入选股票用紧凑列表直接列出**。不再增加逐股下拉筛选或大卡片。

列表优先展示股票代码/交易所、价格、涨跌、量/额、相对前5日均量、换手、行业和概念；支持排序、点击行/上下键换股。K 线与成交量联动，MA 只叠加在图中，最新 MA 值放在图例，下面不再单列 MA 表格。

公式阈值、KDJ 门槛、逐项通过/未通过只留在机器结果，不放在报告主界面。零候选显示空状态，缺来源显示不可用，不拿未命中样本冒充候选。不要加主观买入分、资金流猜测、仓位或收益预测。

需要在对话中预览时，若 `visualize` skill 可用且 `render-manifest.json` 标记允许行内预览，读取它并按其要求将生成的 `report.inline.html` 复制到当前任务可写的持久可视化目录，使用行内引用展示。全市场候选多、体积超限时保留完整 `report.html` 和 Markdown，提供本地文件查看，不截断名单冒充完整结果；没有行内能力时也用此方式。无需为正常出报告修改模板。

## 验证

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
```

修改公式或数据质量门槛时运行边界测试；修改模板时验证所有策略、零候选/缺来源状态、排序、图表、窄屏及非固定日期/股票池。生成报告不可含本次演示的固定股票、固定日期或固定“8只样本”文字。
