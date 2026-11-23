#!/usr/bin/env python3
"""A股微观规则约束与严格因果去重的量化回测引擎（零依赖 Python 3.8+ 标准库）。

核心设计原则（严格遵循 EdsonStock caveats.md 因果检验标准）：
  1) 严禁未来函数（Zero Look-Ahead Bias）：
     所有均线、波动率、分位数阈值只使用截至 T-1 日或 T 日集合竞价的数据（Rolling 滚动窗口），禁止全样本分位数。
  2) A股真实交易微观约束：
     - T+1 交收制度：T 日买入的仓位最早 T+1 日才可卖出；
     - 涨跌停不可成交过滤：
       * 一字涨停（open == high == low 且涨幅达到涨跌停阈值）或开盘即封死涨停时，禁止买入（记为排单失败 missed_limit_up）；
       * 一字跌停（open == high == low 且跌幅达到跌停阈值）时，禁止止损卖出（顺延至开板日成交，真实反映核按钮流动性锁死风险）；
     - 真实交易摩擦成本：
       * 买入手续费：万 1.5 (0.00015)
       * 卖出手续费 + 印花税：万 1.5 + 万 5 (0.00065，2023-08-28 减半后标准)
       * 双边冲击滑点：常规策略默认 10 bps (0.0010)，超短竞价/接力策略默认可设为 15~20 bps
  3) 内置四大策略（含两套游资超短专属策略）：
     - ma_cross       : 放量均线多头趋势策略
     - breakout       : 20日平台放量突破策略
     - mean_revert    : 60日滚动20分位冰点潜伏均值回归策略
     - weak_to_strong : 【游资超短：首板/爆量分歧次日竞价弱转强 (1进2/反包接力)】
                        逻辑：T-1 日为首板涨停（或曾触及涨停炸板/涨幅>=7%且放量>1.5倍均量，且 T-2 日非涨停），
                              T 日集合竞价开盘高开在 [+1.0%, +6.5%] 之间（剔除一字板买不进与水下深低开核按钮），
                              9:25 竞价达标即按开盘价+滑点买入；持仓后只要当日封住涨停则继续锁仓享受连板溢价，
                              一旦断板（收盘未涨停）或持仓满 5 日，于次日开盘立刻兑现离场。
     - first_pullback : 【游资超短：涨停突破后首次缩量回踩5日线（龙回头/N型反包）】
                        逻辑：过去 5 日内出现过涨停板基因，随后回调至 5 日均线附近且成交量萎缩至涨停日 75% 以下，
                              次日开盘不低开（>=-2%）确认承接买入，冲高或持仓 3~4 日止盈止损。
  4) 严格因果事件研究（Event Study with First-Entry Deduplication）：
     连续满足阈值条件的区间只取【首次进入日】作为独立样本，拒绝重叠采样夸大胜率，并输出单侧二项检验近似 p 值。

示例：
  python3 scripts/ashare_backtest.py --code 002771 --strategy weak_to_strong --days 500 --slippage 0.0015
  python3 scripts/ashare_backtest.py --code 002771 --strategy first_pullback --days 500
  python3 scripts/ashare_backtest.py --code 600519 --strategy ma_cross --days 500
"""
import argparse
import json
import math
import urllib.request
from realtime_and_short import normalize_code, http_get


def fetch_daily_qfq(code_raw, count=600):
    """从腾讯拉取前复权日线序列 [date, open, close, high, low, volume]。"""
    pure, pcode, board, limit_pct = normalize_code(code_raw)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (pcode, count)
    raw = json.loads(http_get(url))
    node = (raw.get("data") or {}).get(pcode) or {}
    rows = node.get("qfqday") or node.get("day") or []
    bars = []
    for r in rows:
        bars.append({
            "date": r[0],
            "open": float(r[1]),
            "close": float(r[2]),
            "high": float(r[3]),
            "low": float(r[4]),
            "vol": float(r[5]),
        })
    return pure, pcode, board, limit_pct, bars


def is_limit_up_close(bar, prev_close, limit_pct):
    if prev_close <= 0:
        return False
    limit_px = round(prev_close * (1.0 + limit_pct), 2)
    return bar["close"] >= limit_px - 0.015


def is_touched_limit_up(bar, prev_close, limit_pct):
    if prev_close <= 0:
        return False
    limit_px = round(prev_close * (1.0 + limit_pct), 2)
    return bar["high"] >= limit_px - 0.015


def is_one_word_limit_up(bar, prev_close, limit_pct):
    if prev_close <= 0:
        return False
    limit_px = round(prev_close * (1.0 + limit_pct), 2)
    return abs(bar["open"] - bar["high"]) < 1e-4 and abs(bar["open"] - bar["low"]) < 1e-4 and bar["close"] >= limit_px - 0.015


def is_open_at_limit_up(bar, prev_close, limit_pct):
    if prev_close <= 0:
        return False
    limit_px = round(prev_close * (1.0 + limit_pct), 2)
    return bar["open"] >= limit_px - 0.015


def is_one_word_limit_down(bar, prev_close, limit_pct):
    if prev_close <= 0:
        return False
    limit_px = round(prev_close * (1.0 - limit_pct), 2)
    return abs(bar["open"] - bar["high"]) < 1e-4 and abs(bar["open"] - bar["low"]) < 1e-4 and bar["close"] <= limit_px + 0.015


def normal_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def run_backtest(code_raw, strategy="ma_cross", days=500, fee_buy=0.00015, fee_sell=0.00065, slippage=0.0010):
    pure, pcode, board, limit_pct, bars = fetch_daily_qfq(code_raw, days)
    if len(bars) < 60:
        print("历史K线不足 60 根，无法回测。")
        return

    cash = 1.0
    position = 0.0
    entry_price = 0.0
    entry_idx = -1
    equity_curve = []
    trades = []
    missed_limit_ups = 0
    locked_limit_downs = 0

    # 用于严格因果事件检验（区间首次触发去重）
    in_signal_regime = False
    dedup_events = []

    for i in range(len(bars)):
        b = bars[i]
        prev_c = bars[i - 1]["close"] if i > 0 else b["open"]
        prev2_c = bars[i - 2]["close"] if i > 1 else prev_c

        if i < 25:
            equity_curve.append((b["date"], cash))
            continue

        # 截至 T-1 日的历史窗口 + T 日 9:25 集合竞价开盘价 b["open"]（严禁使用 T 日 high/low/close 决定 T 日开盘买入）
        hist_closes = [bars[j]["close"] for j in range(max(0, i - 60), i)]
        hist_vols = [bars[j]["vol"] for j in range(max(0, i - 20), i)]
        c_prev = hist_closes[-1]
        ma5_prev = sum(hist_closes[-5:]) / 5.0
        ma20_prev = sum(hist_closes[-20:]) / 20.0
        vol_ma5 = sum(hist_vols[-5:]) / 5.0
        vol_ma20 = sum(hist_vols[-20:]) / 20.0

        sorted_c = sorted(hist_closes)
        roll_q20 = sorted_c[int(len(sorted_c) * 0.20)]
        roll_q80 = sorted_c[int(len(sorted_c) * 0.80)]

        buy_signal = False
        sell_signal = False

        if strategy == "ma_cross":
            buy_signal = (ma5_prev > ma20_prev) and (c_prev > ma20_prev) and (vol_ma5 > vol_ma20 * 1.05)
            sell_signal = (ma5_prev < ma20_prev)

        elif strategy == "breakout":
            high_20 = max(bars[j]["high"] for j in range(i - 20, i))
            low_10 = min(bars[j]["low"] for j in range(i - 10, i))
            buy_signal = (c_prev >= high_20 * 0.995) and (hist_vols[-1] > vol_ma20 * 1.3)
            sell_signal = (c_prev <= low_10) or (position > 0 and c_prev < entry_price * 0.93)

        elif strategy == "mean_revert":
            buy_signal = (c_prev <= roll_q20)
            sell_signal = (c_prev >= roll_q80) or (position > 0 and (i - entry_idx) >= 15)

        elif strategy == "weak_to_strong":
            # 游资超短：首板/爆量异动次日竞价弱转强（1进2 / 分歧转一致）
            # 条件1：T-1 日是首板涨停，或曾触及涨停炸板/大涨>=6.5% 且放量 > 1.4 倍 20 日均量（T-2 日非涨停，避免高位接盘）
            bar_t1 = bars[i - 1]
            bar_t2 = bars[i - 2]
            prev3_c = bars[i - 3]["close"] if i > 2 else bar_t2["open"]
            t1_chg = (bar_t1["close"] / prev2_c - 1.0) if prev2_c > 0 else 0.0
            t1_is_zt = is_limit_up_close(bar_t1, prev2_c, limit_pct)
            t1_touched_zt = is_touched_limit_up(bar_t1, prev2_c, limit_pct)
            t2_is_zt = is_limit_up_close(bar_t2, prev3_c, limit_pct)
            t1_vol_surge = bar_t1["vol"] > (vol_ma20 * 1.35)
            t1_upper_shadow = (bar_t1["high"] - bar_t1["close"]) / max(0.01, prev2_c) >= 0.03 and t1_vol_surge
            t1_setup = (not t2_is_zt) and ((t1_is_zt and not is_one_word_limit_up(bar_t1, prev2_c, limit_pct))
                                           or (t1_touched_zt and t1_vol_surge)
                                           or (t1_chg >= limit_pct * 0.65 and t1_vol_surge)
                                           or t1_upper_shadow)
            # 条件2：T 日 9:25 集合竞价高开在 [+0.5%, +9.5%]（弱转强确认，若顶一字涨停则进入下方排单拦截器）
            open_gap = (b["open"] / prev_c - 1.0) if prev_c > 0 else 0.0
            auction_strong = (0.005 <= open_gap <= limit_pct * 1.01)
            buy_signal = t1_setup and auction_strong

            # 卖出规则（T+1 约束下）：只要 T-1 日（持仓日）封死涨停就继续锁仓享受连板；一旦 T-1 日断板（未封涨停）或持仓满 4 日，T 日开盘立刻兑现
            if position > 0 and i > entry_idx:
                prev_held_zt = is_limit_up_close(bars[i - 1], prev2_c, limit_pct)
                sell_signal = (not prev_held_zt) or ((i - entry_idx) >= 5)

        elif strategy == "first_pullback":
            # 游资超短：近 6 日内有涨停基因，随后缩量回踩 5 日/10 日均线企稳，T 日竞价不深低开（龙回头首阴/二阴低吸）
            had_zt_recently = False
            zt_vol = 0.0
            for k in range(max(1, i - 6), i - 1):
                if is_limit_up_close(bars[k], bars[k - 1]["close"], limit_pct):
                    had_zt_recently = True
                    zt_vol = max(zt_vol, bars[k]["vol"])
            t1_not_zt = not is_limit_up_close(bars[i - 1], prev2_c, limit_pct)
            t1_near_ma5 = (ma5_prev * 0.96 <= c_prev <= ma5_prev * 1.03)
            t1_vol_shrink = (zt_vol > 0 and bars[i - 1]["vol"] <= zt_vol * 0.80)
            open_gap = (b["open"] / prev_c - 1.0) if prev_c > 0 else 0.0
            buy_signal = had_zt_recently and t1_not_zt and t1_near_ma5 and t1_vol_shrink and (-0.015 <= open_gap <= 0.035)

            if position > 0 and i > entry_idx:
                prev_held_zt = is_limit_up_close(bars[i - 1], prev2_c, limit_pct)
                t1_ret_from_entry = (c_prev / entry_price - 1.0)
                sell_signal = (not prev_held_zt and ((i - entry_idx) >= 2 or t1_ret_from_entry >= 0.05 or t1_ret_from_entry <= -0.045))

        # 严格因果事件去重记录（超短策略评估 T+2 日收盘相对 T 日开盘的短线爆发胜率，常规策略评估 T+5 日）
        horizon = 2 if strategy in ("weak_to_strong", "first_pullback") else 5
        if buy_signal and not in_signal_regime:
            in_signal_regime = True
            if i + horizon < len(bars):
                fwd_ret = (bars[i + horizon]["close"] / b["open"] - 1.0) * 100.0
                dedup_events.append((b["date"], fwd_ret))
        elif not buy_signal:
            in_signal_regime = False

        # 执行卖出（必须满足 A 股 T+1：i > entry_idx）
        if position > 0 and i > entry_idx and sell_signal:
            if is_one_word_limit_down(b, prev_c, limit_pct):
                locked_limit_downs += 1
            else:
                exec_sell_px = b["open"] * (1.0 - slippage)
                gross_ret = exec_sell_px / entry_price
                cash = position * gross_ret * (1.0 - fee_sell)
                net_trade_ret = (exec_sell_px * (1.0 - fee_sell)) / (entry_price / (1.0 - fee_buy)) - 1.0
                trades.append({
                    "entry_date": bars[entry_idx]["date"],
                    "exit_date": b["date"],
                    "hold_days": i - entry_idx,
                    "ret_pct": round(net_trade_ret * 100, 2),
                })
                position = 0.0
                entry_price = 0.0
                entry_idx = -1

        # 执行买入（检查一字涨停或开盘即涨停不可成交约束）
        elif position == 0 and buy_signal:
            if is_one_word_limit_up(b, prev_c, limit_pct) or is_open_at_limit_up(b, prev_c, limit_pct):
                missed_limit_ups += 1
            else:
                exec_buy_px = b["open"] * (1.0 + slippage)
                position = cash * (1.0 - fee_buy)
                cash = 0.0
                entry_price = exec_buy_px
                entry_idx = i

        cur_eq = cash if position == 0 else position * (b["close"] / entry_price)
        equity_curve.append((b["date"], cur_eq))

    # 绩效汇总计算
    total_days = len(equity_curve)
    final_eq = equity_curve[-1][1]
    total_ret = (final_eq - 1.0) * 100.0
    ann_ret = ((final_eq ** (250.0 / max(1, total_days))) - 1.0) * 100.0
    bench_ret = (bars[-1]["close"] / bars[0]["close"] - 1.0) * 100.0

    peak = 0.0
    max_dd = 0.0
    daily_rets = []
    for idx, (_, eq) in enumerate(equity_curve):
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
        if idx > 0:
            prev_eq = equity_curve[idx - 1][1]
            daily_rets.append(eq / prev_eq - 1.0 if prev_eq > 0 else 0.0)

    avg_d = sum(daily_rets) / max(1, len(daily_rets))
    std_d = math.sqrt(sum((r - avg_d) ** 2 for r in daily_rets) / max(1, len(daily_rets)))
    sharpe = (avg_d / std_d * math.sqrt(250.0)) if std_d > 1e-9 else 0.0

    wins = [t for t in trades if t["ret_pct"] > 0]
    losses = [t for t in trades if t["ret_pct"] <= 0]
    win_rate = len(wins) / max(1, len(trades)) * 100.0
    avg_win = sum(t["ret_pct"] for t in wins) / max(1, len(wins))
    avg_loss = abs(sum(t["ret_pct"] for t in losses) / max(1, len(losses)))
    pl_ratio = avg_win / max(0.01, avg_loss)

    horizon = 2 if strategy in ("weak_to_strong", "first_pullback") else 5
    ev_n = len(dedup_events)
    ev_wins = sum(1 for _, r in dedup_events if r > 0)
    ev_win_rate = ev_wins / max(1, ev_n) * 100.0
    z_stat = (ev_wins - 0.5 * ev_n) / math.sqrt(0.25 * max(1, ev_n)) if ev_n > 0 else 0.0
    p_val = 1.0 - normal_cdf(z_stat)

    print("=== A股真实微观约束回测报告 (%s %s | 策略: %s) ===" % (pcode, board, strategy))
    print("  回测区间: %s ~ %s (%d 交易日) | 涨跌停阈值: ±%.0f%% | T+1 交收约束: 已启用" %
          (bars[0]["date"], bars[-1]["date"], total_days, limit_pct * 100))
    print("  摩擦成本: 买佣万%.1f + 卖佣及印花税万%.1f + 双边滑点 %.0fbps" %
          (fee_buy * 10000, fee_sell * 10000, slippage * 10000))
    print("  ------------------------------------------------------------------------")
    print("  策略累计收益: %+.2f%% (年化 %+.2f%%) | 标的买入持有基准: %+.2f%% | 超额Alpha: %+.2f%%" %
          (total_ret, ann_ret, bench_ret, total_ret - bench_ret))
    print("  最大回撤(MDD): %.2f%% | 年化夏普比率(Sharpe): %.2f" % (max_dd * 100, sharpe))
    print("  交易笔数: %d 笔 | 实盘交易胜率: %.1f%% | 盈亏比: %.2f (均盈 %+.2f%% / 均亏 -%.2f%%)" %
          (len(trades), win_rate, pl_ratio, avg_win, avg_loss))
    print("  A股微观过滤: 一字/开盘涨停排单失败拦截 %d 次 | 一字跌停锁仓延迟卖出 %d 次" %
          (missed_limit_ups, locked_limit_downs))
    print("  ------------------------------------------------------------------------")
    print("  【严格因果检验（滚动窗口无未来函数 + 区间首次进入去重）】")
    print("  独立去重信号事件数: %d 次 | 触发后 %d 日胜率: %.1f%% | 单侧检验 p-value = %.3f (%s)" %
          (ev_n, horizon, ev_win_rate, p_val, "统计显著 p<0.05" if p_val < 0.05 and ev_n >= 8 else "统计不显著，需依赖盈亏比或板块情绪共振"))
    if trades:
        print("\n  最近 5 笔完成交易明细:")
        for t in trades[-5:]:
            print("    买入:%s -> 卖出:%s (%2d日) | 净收益: %+6.2f%%" %
                  (t["entry_date"], t["exit_date"], t["hold_days"], t["ret_pct"]))


def main():
    ap = argparse.ArgumentParser(description="A股真实微观约束与严格因果去重回测引擎")
    ap.add_argument("--code", required=True, help="股票代码，如 002771、600519 或 300750")
    ap.add_argument("--strategy", default="ma_cross",
                    choices=["ma_cross", "breakout", "mean_revert", "weak_to_strong", "first_pullback"],
                    help="策略: ma_cross | breakout | mean_revert | weak_to_strong(首板/放量次日竞价弱转强1进2) | first_pullback(涨停基因缩量回踩5日线)")
    ap.add_argument("--days", type=int, default=500, help="回测历史日线条数（默认 500）")
    ap.add_argument("--slippage", type=float, default=0.0010, help="单边滑点（默认 0.0010 即 10bps）")
    args = ap.parse_args()
    run_backtest(args.code, args.strategy, args.days, slippage=args.slippage)


if __name__ == "__main__":
    main()
