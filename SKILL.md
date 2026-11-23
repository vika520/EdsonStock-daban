---
name: ashare-data-fetcher
description: 专业级 A 股量化数据获取、盘中五档实时分析、超短打板情绪雷达与无偏差回测技能。深度整合 EdsonStock (vika520/edsonstock-data) 15 类盘后结构化数据（ERP 股债利差、两融余额、期指基差与会员持仓、市场宽度 ADL/TRIN/NH-NL、TMT 拥挤度、行业 PE 横截面、EdsonVIX、STH 成本、每日复盘）以及腾讯/新浪/东财/同花顺/通达信(pytdx)/adata/efinance/baostock 多源实时与历史接口。适用于所有 A 股行情查询、盘中五档盘口盯盘、超短连板晋级与游资龙虎榜席位拆解、宏观市场水位判读及 A 股 T+1 量化回测。
---
# A-Share Quant & Ultra-Short Data Fetcher (`ashare-data-fetcher`)

本技能采用**四层专业量化工程架构**，融合 [`vika520/edsonstock-data`](https://github.com/vika520/edsonstock-data) 的 15 类盘后结构化基座、盘中五档实时盘口网关、超短游资打板情绪雷达以及符合 A 股真实交易微观约束（T+1、涨跌停一字板过滤、严格因果去重检验）的回测引擎。

所有内置脚本均采用 **Python 3.8+ 纯标准库（零第三方依赖）** 编写，开箱即用，并兼容 `akshare`、`efinance`、`adata`、`baostock`、`pytdx`/`mootdx` 等开源量化生态。

---

## 一、 四层数据源路由矩阵（按场景精准分流，杜绝单点封 IP）

执行任何 A 股数据查询或分析时，**严禁无脑单调 `akshare` 东财接口**。必须按以下四层架构优先调用本技能 `scripts/` 目录下的生产级脚本：

| 层级 | 业务场景 | 首选执行入口（内置零依赖脚本） | 开源库扩展 / 备选降级源 | 核心防封与口径优势 |
| :--- | :--- | :--- | :--- | :--- |
| **L1 宏观与市场结构层** | ERP 股债利差(5200+日)、两融杠杆、IF/IC/IM 期指基差与前20会员净空单、市场宽度(ADL/TRIN/NH-NL)、TMT 成交占比、行业 PE 横截面、EdsonVIX、STH 成本、每日复盘 | `python3 scripts/query.py --what <name>` | `--local <dir>` 读本地 `wolf-cycle/data/*.json` 或运行 `scripts/fetch_*.py` 自取 | 1 小时本地缓存、秒级拉取 `vika520/Edsonstock` 盘后权威 JSON，自带三档口径锁与因果检验结论 |
| **L2 盘中实时与多周期 K 线层** | 个股/ETF/指数实时价格、五档买卖盘口、外盘/内盘主动差、委比、量比、换手率、涨跌停价、涨停封成比、日/周/5m/15m/30m/60m K线 | `python3 scripts/realtime_and_short.py --action quote` 或 `--action kline` | `pytdx` / `mootdx`（通达信直连）、`efinance` (`ef.stock.get_quote_history`) | 直连腾讯(`qt.gtimg.cn`/`ifzq.gtimg.cn`)与新浪(`hq.sinajs.cn` 带 Referer)，**永不封 IP**，支持毫秒级五档盘口与封单比计算 |
| **L3 超短打板与游资龙虎榜层** | 当日涨停池/跌停池/炸板池、连板身位梯队、首封时间、炸板次数、封成比/封流比、主线行业集中度、龙虎榜买卖前五营业部及游资/量化/机构/拉萨席位画像 | `python3 scripts/realtime_and_short.py --action radar` 或 `--action billboard` | 同花顺历史涨停池 (`limit=200`)、`adata` (`1nchaos/adata`)、`akshare` | 东财 `push2ex` / `datacenter` 默认走 **`curl` 子进程隔离防封**；休市或非交易日自动无缝降级至 EdsonStock 250 日连板晋级历史库 |
| **L4 无偏差量化回测层** | 策略历史回测、超额 Alpha、最大回撤、夏普比率、一字涨停排单失败拦截、一字跌停锁仓、严格因果去重 p 值检验 | `python3 scripts/ashare_backtest.py --code <code> --strategy <name>` | `baostock`（证券宝，含复权因子与历史成分股）、本地 CSV / SQLite / 通达信 `.day` | 强制执行 A 股 **T+1 交收**、板块差异化涨跌停（10%/20%/30%/5%）、真实印花税佣金滑点、滚动窗口无未来函数与区间首次触发去重 |

---

## 二、 标准作业流程（SOP）与命令速查

> **路径提示**：在沙盒中运行内置脚本前，先定位本技能所在目录（或将本技能的 `scripts/` 与 `references/` 复制到工作区运行）。

### 1. 宏观水位与大盘结构先行（Top-Down Context）
无论用户询问大盘复盘、板块配置还是单只个股买卖点，**第一步先查市场水位**（用时 < 1 秒）：
```bash
# 查看全部 15 类结构数据清单
python3 scripts/query.py --what list

# 查股债利差 ERP（当前分位、±2σ 通道、90分位低估线距离）
python3 scripts/query.py --what erp

# 查市场宽度（ADL / 麦克莱伦 / NH-NL / TRIN）、TMT 拥挤度、期指基差与会员持仓、两融余额
python3 scripts/query.py --what flow --tail 5
python3 scripts/query.py --what tmt
python3 scripts/query.py --what derivatives --tail 5
python3 scripts/query.py --what leverage --tail 5

# 查 EdsonVIX 恐慌指数、STH 短期持仓成本、行业 PE 估值横截面、最新完整 15 栏复盘
python3 scripts/query.py --what vix
python3 scripts/query.py --what sth
python3 scripts/query.py --what valuation --top 15
python3 scripts/query.py --what review --tail 1
```
- **本地数据源无缝对接**：若用户本地已有 `wolf-cycle/data` 目录，传入 `--local /path/to/wolf-cycle/data` 即可零网络延迟读取本地 JSON。

### 2. 超短打板、情绪周期与游资龙虎榜复盘（Ultra-Short & Hot Money）
当用户进行超短盯盘、连板接力、龙头战法或龙虎榜复盘时，调用 `realtime_and_short.py`：
```bash
# 1) 超短情绪周期雷达：自然涨停/跌停/炸板率、最高连板梯队、首封时间、封成比、涨停主线行业集中度
python3 scripts/realtime_and_short.py --action radar

# 2) 全市场最新龙虎榜主力净买排名 + 买卖前五席位穿透（自动标记：顶级游资/机构/北向/高频量化/拉萨天团）
python3 scripts/realtime_and_short.py --action billboard --top 10

# 3) 单只个股龙虎榜历史与买卖前五席位博弈拆解
python3 scripts/realtime_and_short.py --action billboard --code 600519
```
- **席位博弈判读准则**（详见 `references/hot_money_seats.md`）：
  - **顶级游资合力**（如六一中路、呼家楼、章盟主、方新侠、作手新一、陈小群等）且买一独食度 $< 12\%$：次日接力溢价高；
  - **高频量化扎堆**（华鑫上海分公司、中金上海分公司、国君总部、瑞银花园石桥路等买卖双边同现）：警惕次日集合竞价或早盘冲高算法兑现砸盘；
  - **拉萨天团霸榜**（东财拉萨团结路/东环路/南环路占买榜 3 席以上且主力游资在卖榜净卖出）：典型散户接盘高潮尾声，严防次日核按钮。

### 3. 个股盘中实时五档盘口与多周期量价分析（Real-Time L1 & Multi-Timeframe K-Line）
```bash
# 批量查询实时五档委买委卖盘口、内外盘主动差、五档委比、换手率、量比、涨跌停价、涨停封成比/封流比
python3 scripts/realtime_and_short.py --action quote --codes 600519,300750,000001

# 拉取多周期 K 线（支持 day / week / m60 / m30 / m15 / m5）及 MA5/MA20
python3 scripts/realtime_and_short.py --action kline --code 600519 --period day --count 30
python3 scripts/realtime_and_short.py --action kline --code 300750 --period m15 --count 25
```

### 4. A 股真实微观规则约束回测（Bias-Free Backtesting）
当用户要求回测某个技术指标、打板策略或均值回归信号时，运行或基于 `scripts/ashare_backtest.py` 扩展：
```bash
# 运行内置策略回测（ma_cross | breakout | mean_revert | weak_to_strong: 首板/试盘次日竞价弱转强1进2 | first_pullback: 涨停缩量回踩5日线）
python3 scripts/ashare_backtest.py --code 600519 --strategy ma_cross --days 500
python3 scripts/ashare_backtest.py --code 300750 --strategy breakout --days 500 --slippage 0.0015
```
**自定义策略回测的 5 条硬性工程纪律**：
1. **T+1 交收锁**：当日（Index `i`）买入的持仓，绝不允许在当日平仓或日内止损，最早只能于 `i+1` 日开盘或盘中卖出。
2. **一字板流动性过滤**：
   - 买入信号触发日若标的开盘一字涨停（`open == high == low == limit_up`），实盘排板大概率无法成交，必须记为 `missed_limit_up` 跳过买入，**严禁按涨停价理想化计入持仓**；
   - 持仓触发止损日若标的开盘一字跌停（`open == high == low == limit_down`），必须锁仓顺延至开板日成交，真实反映超短核按钮风险。
3. **板块差异化涨跌停阈值**：主板 `±10%`（ST `±5%`）、科创板(`688`)/创业板(`300/301`) `±20%`、北交所(`8/4/920`) `±30%`。
4. **真实交易成本**：默认买佣 `万1.5`，卖佣 `万1.5` + 印花税 `万5`（合计 `万6.5`），双边冲击滑点 `10~20 bps`。
5. **零未来函数与区间首次触发去重**：所有分位数/通道阈值只能使用截至当日的滚动窗口（Rolling）或扩展窗口（Expanding）；连续满足条件的区间必须按**首次进入日去重（First-Entry Deduplication）**并计算单侧检验 `p-value`。

---

## 三、 数据口径红线与因果检验纪律（必读，详见 `references/caveats.md`）

在向用户输出任何分析报告、回测结论或复盘表格时，**必须显式标注数据所属的三档口径**，严禁混用口径或用代理数据冒充真实资金流：

1. **【真实公开数据】**（可直接引用，注明披露时滞）：
   - 腾讯/新浪盘中五档盘口与日/分钟 K 线（实时或收盘）；
   - 两融余额与融资买入额（T+1 交易所披露）；
   - 中金所 IF/IH/IC/IM 期指行情、IO/HO/MO 期权 PCR、前 20 会员持仓排名（盘后官方 XML）；
   - 东财当日涨跌停池/炸板池、同花顺历史涨停池（`limit=200`）、限售解禁、龙虎榜营业部明细、中债 10Y 收益率。
2. **【量价代理】**（引用时必须标注“量价代理，非真实资金流”）：
   - 各类软件的“主力净流入 / 资金方向代理”（本质是 $\sum(\text{涨跌方向} \times \text{成交额})$，非真实委托流）；
   - TMT 成交占比（四大行业成交额 ÷ 两市成交额，代表交易拥挤度而非资金净进出）；
   - 市场宽度指标（ADL / 麦克莱伦 / NH-NL / TRIN / OBV）、STH 筹码衰减成本、EdsonVIX 自编方差互换恐慌指数。
3. **【确认不可得】**（免费公开源不存在，必须如实告知，严禁伪造或插值）：
   - 北向资金日度净买入（2024-08-19 起交易所已停止披露日度净买入，严禁继续构建北向日度净流入因子）；
   - ETF 交易所日度官方真实份额历史（仅基金季报有真实申赎，日度市值反推含折溢价误差）；
   - Level-2 逐笔委托队列与真实主动大单拆单明细；
   - 东财涨跌停池的历史回补（东财 `getTopicZTPool` 的 `date` 参数对历史日失效，历史涨停回溯只能用同花顺接口或 `history.json`）。
4. **【4 个经严格因果检验不显著的信号】**（不得向用户吹嘘为高胜率圣杯）：
   - 情绪“双冰点”后次日大盘上涨（严格因果胜率 55.6% vs 基准 52.5%，$p \approx 0.45$ 不显著）；
   - 威科夫 Phase C 弹簧兑现率（近 3 年兑现率仅 25%，$p = 0.256$）；
   - ERP $\ge 90$ 分位或 $\ge 75$ 分位后短期反弹（剔除全样本未来函数与重叠采样后统计不显著）。

---

## 四、 踩坑速查与合规声明（详见 `references/sources.md` & `references/schema.md`）

- **东财 `push2*` 断连修复**：Python `urllib`/`requests` 请求东财常被代理重置连接，务必使用 `realtime_and_short.py` 中的 `http_get(..., use_curl=True)` 走 `curl` 子进程。
- **行业 PE 两套口径不可直接混比**：`extras.json` 中的 `valuation.industry`（219 个东财板块横截面）与 `history.json` 中的 `valuation.daily`（127 个报表行业历史中位数）样本池不同，严禁跨表直接相减。
- **免责声明**：在所有分析报告、选股复盘与回测输出的末尾，必须附上声明：`AI 量化数据与风格模拟，所有数据源自公开接口统计，不构成任何投资建议。`
