# 趋势波段 · 价值动量（1~4 周）— 设计与实施规划

> 状态：**规划中，未实现**。本文档是 Gemini 投研方案经**数据源实测校准**后的可执行版本。
> 与超短打板池（`auto_pick_top10.py`）完全独立，互不干扰。

## 0. 方案校准结论（先读这个）

Gemini 的五层漏斗投研框架是合理的，但它对数据源的假设**有 4 处与实测不符**。下表是 2026-11-25 的真实探测结果，实施必须以此为准：

| 方案假设 | 实测结果 | 校准 |
|---|---|---|
| 东财 `push2.eastmoney.com` 板块列表 `clist/get` + `push2his` 板块 K 线 | **稳定 `HTTP=000`（连接被重置）**，东财对板块类接口单独封锁；个股 `push2` 接口正常 | **弃用**，改用腾讯板块接口 |
| `flow.json` 含「麦克莱伦 OSC / ADL 腾落线」 | 字段**不存在**。实际字段：`up/dn/fl/n, uv/dv, nh60/nl60/nh250/nl250, e60/e250/ew/exd` | 宽度维度改用 **NH-NL（`nh60-nl60`、`nh250-nl250`）+ 涨跌家数比** |
| 东财 `RPT_LICO_FN_CPD` 用 `REPORT_DATE` + `ROEJQ` | 列名是 `REPORTDATE`（无下划线）；`ROEJQ` 不存在 | 修正列名；**ROE 改从腾讯成分股接口取或用净利/净资产估算** |
| `workbench.json` 作指数日线源 | 908KB，GitHub raw 连续 SSL EOF 失败 | 指数日线改从**腾讯 `fqkline`** 直取（已验证 5 个指数全通） |
| （方案未提及） | **腾讯板块接口 `proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank`** 可用：`board_type=hy`(31 行业) / `gn`(200 概念) / `dy`(31 地域)，自带 `zdf_d5/d20/d60/w52`（各期涨幅）、`zljlr_d5/d20`（主力净流入）、`ltsz`、`turnover`、`hsl` | **这是轮动引擎的主数据源**，方案里要手算的 RS 动量和资金关注度，腾讯直接给现成的 |
| （方案未提及） | 腾讯成分股接口 `rank/hs/getBoardRankList?board_code=pt...` 可用，自带 `pe_ttm`、`pn`(PB)、`ltsz`、`hsl` | 板块内广度 + 个股估值一次拉取 |

## 1. 策略框架（保留 Gemini 五层漏斗）

目标：与打板池**低相关性互补**。打板赚情绪溢价；波段赚「宏观顺风 + 行业轮动 + 业绩安全垫 + 右侧趋势延续」。

### 第 1 层：市场择时（0~100 分 → 三档）

| 维度 | 权重 | 数据源（实测可用） | 口径 |
|---|---|---|---|
| 大盘趋势 | 40 | 腾讯 `fqkline`：沪深300/中证500/创业板 vs MA20/MA60 + MA20 斜率 | 真实公开数据 |
| 市场宽度 | 30 | `flow.json`：`(nh60-nl60)` 与 `(nh250-nl250)` 5 日均值 + `up/(up+dn)` | 量价代理 |
| 衍生品/杠杆 | 20 | `extras.json → derivatives`（IF/IC/IM 基差）、`→ leverage`（两融 5 日变化） | 真实公开数据 |
| 估值背景 | 10 | `erp.json → snapshot.historyPercentile`，**仅作仓位上限锚，不作触发器** | 真实公开数据 |

- **进攻期 ≥65**：仓位 60~80%，推 6~8 只
- **震荡轮动 40~64**：仓位 30~50%，推 3~5 只，只做贴 MA20 低吸
- **防守避险 <40**：仓位 0~20%，`picks` 允许为空，但 `regime`/`sectorRotation` 必须有效

**纪律**：遵守 `references/caveats.md`，**不用**已被证伪的「ERP≥90 分位抄底」「情绪双冰点」作短期择时。

### 第 2 层：行业轮动（数据源已换为腾讯）

每个板块计算：

- **RS 动量（45%）**：腾讯直接给 `zdf_d5/d20/d60`，减基准（中证800 同期涨幅）得超额
- **资金关注（35%）**：`zljlr_d5/d20`（主力净流入）符号与强度 + `turnover` 占比变化
- **拥挤度护栏（20% + 熔断）**：`tmt.json` TMT 占比高位 / 板块 5 日涨幅 > 15% 且主力净流入拐头 → `【拥挤过热预警】`
- **估值锚**：成分股接口 `pe_ttm` 中位数 vs `extras.json → valuation.industry`

轮动阶段：`启动期`（RS 刚转正 + 低拥挤）→ `主升期`（RS 前 20% + 广度高）→ `高潮/退潮期`（高拥挤或 RS 连续 3 日走弱）。

**待补**：方案说的「板块内站上 MA20 占比」需逐股拉日线，200 概念 × 平均 50 只 = 1 万次请求，**不可行**。降级为「成分股当日上涨占比」（接口直接给 `zdf`）。

### 第 3 层：基本面排雷（东财 datacenter-web，已验证可用）

**硬否决**：
- ST / 上市 < 180 交易日 / 近 15 日有 ≥3 连板
- 20 日日均成交额 < 2 亿 或 流通市值 < 40 亿
- 未来 20 交易日解禁占总股本 > 3%（`RPT_LIFT_STAGE`，字段 `FREE_DATE/FREE_SHARES/TOTAL_RATIO`）
- 业绩预告为预减/略减/首亏/续亏（`RPT_PUBLIC_OP_NEWPREDICT`，字段 `PREDICT_TYPE`）

**Value-Quality 打分（100）**：
- 盈利质量 35：经营现金流/净利 > 0.8
- 成长景气 35：`RPT_LICO_FN_CPD` 的 `YSTZ`（营收同比）> 10、`SJLTZ`（净利同比）> 15
- 估值性价比 30：`pe_ttm` 行业内分位 < 65% 或 PEG 0.3~1.5

### 第 4 层：技术买点（腾讯日线 150 根 + 周线 40 根，已验证）

右侧前提：周收盘 > 周 MA10 且周 MA20 向上；日 MA20 > MA60；MA20 5 日斜率 ≥ 0。
**禁追高**：当日涨停 / 涨幅 > 7% / 偏离 MA20 > +12% → 只进观察池。

三形态：**A** MA20 回踩低吸（RSI 42~58）/ **B** VCP 突破回踩（ATR10/ATR40 < 0.85）/ **C** Stage-2 右侧启动。

### 第 5 层：交易计划与退出

- 介入区间 `[MA20×0.99, MA20×1.03]`
- 硬止损 `max(Entry − 2×ATR14, 前低×0.985)`
- 浮盈 +8% → 保本；+15% 或偏离 MA20 > 18% → 追踪止盈（收盘破 MA10 或回撤 2.5×ATR14）
- 单票风险 ≤ 净值 1.5%

## 2. 工程架构

### 新增脚本（均纯标准库）

| 文件 | 职责 | 关键依赖 |
|---|---|---|
| `scripts/fetch_fundamentals.py` | 财务/预告/解禁，按周缓存到 `data/fundamentals_cache.json` | 东财 `datacenter-web` via `http_get(use_curl=True)` |
| `scripts/sector_rotation.py` | 腾讯板块排名 → RS/资金/拥挤度/阶段 | `proxy.finance.qq.com/.../pt/getRank` + `rank/hs/getBoardRankList` |
| `scripts/auto_pick_swing.py` | 主引擎：择时 → 轮动 → 排雷 → 形态 → 交易计划 → 账本 | 上面两个 + 腾讯 `fqkline` |
| `scripts/ashare_backtest.py`（扩展） | 新增 `trend_pullback` / `vcp_breakout` / `stage2_start` | 现有回测框架 |

### 数据契约：`web/swing_picks.json`

独立于 `daily_picks.json`。结构见 Gemini 方案第二节，**字段调整**：
- `regime.factors.breadth` 改为 `nh60-nl60` 口径
- `sectorRotation[].rs5/rs10/rs20` 来自腾讯 `zdf_d5/...` 减基准
- `sectorRotation[]` 去掉 `breadthAboveMa20`，改为 `upRatio`（成分股上涨占比）
- 每只 pick 必带 `dataTierNote` 标注【真实公开数据】/【量价代理】

### 调度与前端

- `live_signal_server.py`：新增 `/api/swing_picks`；盘后 **15:20** 初选 + **18:00** 终选
- `.github/workflows/swing-picks.yml`：UTC `07:25` / `10:05`
- `web/index.html`：新 Tab `tab-swing`，四块：择时红绿灯 / 轮动雷达 / 精选池+交易计划卡 / 追踪账本

## 3. 里程碑（严格按序，回测先行）

| Step | 内容 | 验收标准 | 预估 |
|---|---|---|---|
| **S1 回测先行** | `ashare_backtest.py` 加 3 个波段策略；沪深300/中证500 各抽 20~30 只，近 3 年滚动回测 | 含印花税/滑点/T+1/去重；**先确认超额与夏普达标再上线**，不达标就调参或放弃 | 中 |
| **S2 数据模块** | `fetch_fundamentals.py` + `sector_rotation.py` | 单次跑通，输出字段完整率 > 95%，口径标签齐全 | 小 |
| **S3 主引擎** | `auto_pick_swing.py` → `swing_picks.json`；接入 server + Actions | Actions 连续 3 天成功；账本可追踪 | 中 |
| **S4 前端** | `tab-swing` 四块 + 腾讯实时价联动 buyZone/stopLoss | Playwright 断言通过，5 时区验证 | 中 |

**S1 是硬门槛**：Gemini 方案里所有阈值（MA20 偏离 ±、ATR 倍数、PEG 区间）都是拍脑袋给的，**没有回测验证就不许进实盘规则**。

## 4. 已知风险

1. **腾讯板块接口无正式文档**，字段可能变动；需加字段校验 + 失败降级（降级到 `hybk` 行业口径）。
2. **成分股广度降级**：无法算「站上 MA20 占比」，只能用当日涨跌占比，轮动精度下降。
3. **ROE 缺失**：东财 `RPT_LICO_FN_CPD` 无 `ROEJQ`，需另找源或用「净利/净资产」估算并标注。
4. **财务数据滞后**：季报口径，Q1~Q3 之间近 4 个月无更新；业绩预告是补充但有覆盖偏差。
5. **`workbench.json` 不稳定**：不用它做实时源。
