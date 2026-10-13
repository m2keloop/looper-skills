---
name: a-share-policy-screen
description: "lan选股策略：默认运行 B1、B1 周线、年线金叉回踩。日线B1筛近两周量能异动后2—3天缩量并保持结构的候选，按形态分排序；复用缓存生成策略Tab、紧凑全量列表和联动量价图。用于收盘选股，不用于自动交易或收益回测。"
---

# lan选股策略

当前选股范围为B1、B1周线、年线金叉回踩。日线B1按用户要求优化为异动后缩量形态v2.1，基本入选与高分形态分开；周B1和其他公式保留Go提交`5ed70450`文档口径，不声称优化后的日线B1等价于原Go实现。地量、B1_V2、对数乖离率不再参与默认选股或日终任务；其历史公式和结果仍可复核，只有用户明确点名时才可手动运行。B2 已移除，不执行、不展示，也不接受其命令参数。先读 [策略规则](references/policies.md)。行情适配自 a-stock-data v3.10.0，脚本和离线图表随本 skill 提供。

日线B1要求近10根有效日K内至少一次倍量收涨，紧接两根或三根的平均量≤异动量80%，之后每根收盘均守住异动最低价下方3%的容差线；完成后继续观察，超期或明显破位才失效。逐日≤60%的深缩量、严格守线用于加分，小幅偏离只扣分，不与基本门槛混淆。保留原J、市值、板块及大跌门槛。0—100形态分只排序图形贴合程度，不是上涨概率，不作为新增硬门槛，不给周B1/年线混合评分。详细公式、权重与版本见策略规则。

## 执行

用户未指定策略，或使用 `--policies all` 时，只分别运行 `B1`、`B1_WEEKLY`、`MA_GOLDEN_CROSS_FIRST_PULLBACK` 三种；不自行追加其他策略。点名单策略时单独运行。不因旧名称或上次运行 B1 改变默认范围。未指定日期时确认上海时间最近已收盘交易日，非交易日没有新数据则复用已有结果。默认当前 ACTIVE 沪深 A 股全池，不含北交所；全池获取失败不能改成熟悉的小样本。

所有运行数据保存在用户工作区。以下路径相对于本 skill；实际运行用脚本绝对路径。手动与定时任务使用同一 `--root` 和统一入口：

```bash
python3 scripts/run_daily.py --root OUTPUT/daily-selection --date latest
# 显式指定当前三策略，适用于日终自动任务
python3 scripts/run_daily.py --root OUTPUT/daily-selection --date latest --policies B1,B1_WEEKLY,MA_GOLDEN_CROSS_FIRST_PULLBACK
# 一次性导入已有真实行情，计算和报告全部离线
python3 scripts/run_daily.py --root OUTPUT/daily-selection --date YYYY-MM-DD --import-input PRIOR/input.json --offline
# 单策略结果使用独立目录，行情仍共享
python3 scripts/run_daily.py --root OUTPUT/daily-selection --date YYYY-MM-DD --policies B1 --run-dir OUTPUT/b1-run --offline
# 用户明确只看主板时，采集前缩小股票池并复用逐股检查点
python3 scripts/run_daily.py --root OUTPUT/daily-selection --date latest --policies B1_WEEKLY --main-board-only --run-dir OUTPUT/b1-weekly
```

计算、存储和报告只需 Python 标准库。联网依赖缺失时在工作区建 venv，安装 `scripts/requirements-data.txt`，不更改系统 Python。首次没有缓存时统一入口调用 fetch_data 建库；已有缓存按交易日增量追加。同日有效快照重跑行情请求为 0。已完成结果不覆盖，换参数或移除旧版策略后重新计算时指定新的 `--run-dir`。公式版本改变时统一入口在当日目录的`formula-版本摘要/`保存新结果并复用原快照，之后同版本同日重跑直接复用；指定目录有旧公式结果时拒绝覆盖。

## 缓存与数据质量

同一目标日、股票池、供应商和复权口径共用一份不可变快照；当前三策略、周线、MA 和报告均在本地读取。运行锁防止相同缓存根目录的采集任务重叠。

baostock 股票池连接失败时可切换至沪深交易所当前完整上市名单；核验分页总数、上市/退市日期及唯一成员，并保存实际来源。任一市场名单不完整则任务失败，不沿用旧名单冒充当前池，也不缩成部分市场。

只补缺失交易日并验证重叠 OHLCV；复权变化、无重叠或真正缺少可取得的历史才刷新对应股票。已确认从上市起完整取得的自然短历史不因少于 741 根重复拉取，年线仍标记不足。失败成员留在全池分母。修订创建新历史版本，旧快照数据不覆盖。详见 [数据协议与缓存](references/data-contract.md)。

保守收盘口径保持：最后 K 必须等于目标日，OHLCV、复权、成交量单位有证据，必要股本和上市板块有来源。缺 K 不填补，内部停牌只按公告证据豁免；目标日停牌不作为当日候选。区分 hit、not_hit、not_evaluated、failed；部分结果不能称全市场成功零候选。

股本和行业缓存保留真实来源与 as_of；新成员和发现复权变化时更新股本。正常增量沿用最近采集披露值，不能声称逐日重新验证全部股本或忽略其时效；供应商变更、股本事件需单独补字段，不由市值倒推。行业/概念等展示缺失不触发 K 重取。

请求使用连接复用及统一请求开始间隔，默认 0.25 秒，串行执行。分页和备用入口也计入限流。超时与连续失败保留检查点、停止无限重试。不要为提速额外多开进程或降低请求间隔；`--retry-failures`只在快照发布前重试失败检查点。

指标继续从各策略的专属窗口初始化。不能拿长历史 KDJ/EMA 代替 B1 的 21 根或把昨日滑动窗口 EMA 直接续接到今日。黄白线与乖离率回抽默认关闭，按需用 `--yellow-filter`、`--require-pullback`。

## 离线计算与报告恢复

```bash
python3 scripts/screen.py --input RUN/input-manifest.json --out RUN/result --policies all
# schema 1.0 的旧 input.json 仍可直接计算
python3 scripts/screen.py --input PRIOR/input.json --out NEW/result --policies all
# 报告失败时，仅重建报告，行情请求与策略重算均为零
python3 scripts/screen.py --out RUN/result --rebuild-report
```

统一入口写轻量 input-manifest.json，长期数据在共享 _cache/market.sqlite；结果保存后才生成报告，报告失败不丢失逐股计算。result/results.json 保存所有逐股状态、原因和关键指标，通过 input_ref 与摘要引用行情，不重复嵌入全池长历史。已保存的旧策略结果可离线重建原报告，不加入新日终结果。

report.html 和 report.md 保留全部候选。view-model.json.gz 是可复用列式展示数据；大报告在 HTML 内压缩并本地解码，不请求行情或 CDN。旧浏览器不支持原生 gzip 解码时查看完整 report.md 或改用支持的浏览器。查看 results.json 和 run-metrics.json 后总结结果，不从截图猜数量。只有报告核验完成才更新 latest。

报告依照 [模板要求](references/report-template.md)：策略之间 Tab，同策略紧凑全量列表；股票代码/交易所、价格、涨跌、量/额、相对前5日均量、换手、行业与标签。排序、点击行和上下键联动；MA 只在量价图和图例显示。日线B1 v2增加形态分与异动日期，默认按分数降序，在日量价图标记异动和缩量区间；原B1不补造评分，v2.0沿用已保存的原分数。公式检查不放主界面，不生成仓位或收益预测。

行内预览仅在 render-manifest.json 允许且 visualize 能力可用时使用该 skill。体积大则打开完整 HTML、提供本地链接，不裁剪名单冒充全量。

历史日期使用当前池、当前缓存股本与最新取得的前复权历史，不是无前视回测；历史回测需另取当时信息。

## 验证

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
```

改动公式或质量门槛运行边界测试；采集缓存验证自然短历史、复权变化、不可变修订、限流、锁和断点恢复。默认范围验证两个入口及`--policies all`只输出当前三策略；B1验证紧接均量时序、80%均量和3%价格边界、60%只影响分数、2/3天与10根边界、失效后不复活、分数单调性与仅作用于日线、旧公式结果不复用为新公式；报告验证完整名单、零候选与覆盖不足、排序、日周图、MA、窄屏及离线打开。
