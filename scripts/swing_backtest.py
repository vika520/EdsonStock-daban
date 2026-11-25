#!/usr/bin/env python3
"""趋势波段策略回测引擎（零依赖 Python 3.8+ 标准库）— 对应 references/swing_design.md 的 S1。

与 ashare_backtest.py（超短打板回测）独立，互不影响。

三个波段策略（持仓 5~20 交易日）：
  trend_pullback : 形态 A · MA20 回踩低吸。右侧前提 + 前期放量上涨 + 近 3~8 日缩量回调 + 回落至 MA20 ±3.5% + 止跌 K 线
  vcp_breakout   : 形态 B · 波动率收缩突破回踩。20~60 日箱体 + ATR10/ATR40 < 0.85 + 放量温和突破（非涨停）
  stage2_start   : 形态 C · 底部反转右侧启动。长期筑底后放量站上 MA60/MA120 + MA20 金叉

严格纪律（继承 caveats.md）：
  1) 零未来函数：所有指标只用截至 T-1 日的数据；T 日按开盘价 + 滑点买入，不用 T 日 high/low/close 决策
  2) 禁追高：T-1 日涨停 / 涨幅 > 7% / 收盘偏离 MA20 > +12% → 不给信号
  3) A 股约束：T+1、一字涨停不可买、一字跌停不可卖、印花税 + 佣金 + 滑点
  4) 退出：ATR 硬止损 / +8% 保本 / +15% 追踪止盈 / 最长 20 日 / 放量破 MA20 两日不收回
  5) 评估口径：波段看【相对基准的超额收益 + 盈亏比 + 卡玛】，不用「胜率 > 50%」做二项检验（高赔率低胜率是常态）

用法：
  单只：  python3 scripts/swing_backtest.py --code 600519 --strategy trend_pullback
  横截面：python3 scripts/swing_backtest.py --sample hs300 --strategy all --n 30 --out swing_report.json
"""
import argparse
import json
import math
import os
import pathlib
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from realtime_and_short import normalize_code, http_get

FEE_BUY = 0.00015
FEE_SELL = 0.00065
SLIPPAGE = 0.0010
MAX_HOLD = 20

# 腾讯 fqkline WAF 保护：横截面回测会对同一接口发数百次请求，过快会触发 waf.tencent.com 拦截（返回 HTML 跳转页，封 10~60 分钟）。
# 实测：~500 次/20 分钟触发封禁。全局节流阀：两次请求间隔 ≥ MIN_INTERVAL 秒（120 只 ≈ 1 分钟）；失败重试 RETRY 次，指数退避。
_MIN_INTERVAL = 0.5
_retry_lock = threading.Lock()
_last_call = [0.0]
_CACHE_DIR = pathlib.Path(os.environ.get("EDSONSTOCK_CACHE", str(pathlib.Path.home() / ".cache" / "edsonstock"))) / "kline"
_CACHE_TTL = 6 * 3600


def _throttled_get(url, retries=3):
    """带全局限速 + 指数退避重试的 GET，返回响应文本。

    显式识别腾讯 WAF 拦截页（返回 HTML 跳转 waf.tencent.com/501page.html 而非 JSON）——
    这种情况重试无意义，直接抛错让调用方停止，避免把 120 只全跑成空结果。
    """
    for attempt in range(retries):
        with _retry_lock:
            wait = _MIN_INTERVAL - (time.time() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        try:
            txt = http_get(url)
            if txt and txt.strip():
                head = txt.lstrip()[:200].lower()
                if head.startswith("<!doctype") or head.startswith("<html") or "waf.tencent.com" in head:
                    raise RuntimeError("腾讯 WAF 拦截（请求过于频繁），请等待 10~30 分钟后重试")
                return txt
        except RuntimeError:
            raise
        except Exception:
            pass
        time.sleep(0.8 * (2 ** attempt))
    raise RuntimeError("请求失败（限流或网络）: %s" % url[:90])


def _cached_kline(key, url):
    """日线本地缓存（6 小时），同一天内重复回测不再打接口。"""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = _CACHE_DIR / ("%s.json" % key)
    if f.exists() and time.time() - f.stat().st_mtime < _CACHE_TTL:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    raw = json.loads(_throttled_get(url))
    try:
        f.write_text(json.dumps(raw), encoding="utf-8")
    except Exception:
        pass
    return raw


# ---------------------------------------------------------------- 指标计算（全部只用 T-1 及之前）

def sma(vals, n, idx):
    """vals[idx-n+1 .. idx] 的简单均值；不足 n 返回 None。"""
    if idx + 1 < n:
        return None
    return sum(vals[idx - n + 1: idx + 1]) / float(n)


def ema(vals, n, idx):
    """从窗口起点递推到 idx 的 EMA。"""
    if idx + 1 < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(vals[idx - n + 1: idx + 1]) / float(n)
    for j in range(idx - n + 1, idx + 1):
        e = vals[j] * k + e * (1 - k)
    return e


def atr(bars, n, idx):
    """ATR(n) 截至idx（含）。"""
    if idx + 1 < n + 1:
        return None
    trs = []
    for j in range(idx - n + 1, idx + 1):
        h, l, pc = bars[j]["high"], bars[j]["low"], bars[j - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / float(n)


def rsi(closes, n, idx):
    """RSI(n) 截至 idx（含），Wilder 平滑。"""
    if idx + 1 < n + 1:
        return None
    gains, losses = [], []
    for j in range(idx - n + 1, idx + 1):
        d = closes[j] - closes[j - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    ag = sum(gains) / n
    al = sum(losses) / n
    if al < 1e-12:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def slope(vals, n, idx, k=5):
    """MA(n) 最近 k 日斜率（每日平均变化，绝对值）。"""
    pts = []
    for j in range(idx - k + 1, idx + 1):
        m = sma(vals, n, j)
        if m is None:
            return None
        pts.append(m)
    return (pts[-1] - pts[0]) / float(k - 1)


# ---------------------------------------------------------------- 策略信号（i 为 T 日，信号只用 < i 的数据）

def _right_side_ok(bars, i):
    """右侧趋势前提（用 T-1 及之前）：MA20 > MA60，MA20 五日斜率 ≥ 0，收盘在 MA20 上方。"""
    closes = [b["close"] for b in bars]
    t = i - 1
    ma20, ma60 = sma(closes, 20, t), sma(closes, 60, t)
    if ma20 is None or ma60 is None:
        return False
    if ma20 <= ma60:
        return False
    sl = slope(closes, 20, t, 5)
    if sl is None or sl < 0:
        return False
    return closes[t] > ma20 * 0.97


def _no_chase(bars, i, limit_pct):
    """禁追高（用 T-1）：非涨停、涨幅 ≤ 7%、偏离 MA20 ≤ +12%。"""
    t = i - 1
    c, pc = bars[t]["close"], bars[t - 1]["close"]
    chg = c / pc - 1.0 if pc > 0 else 0.0
    if chg >= limit_pct - 0.005 or chg > 0.07:
        return False
    ma20 = sma([b["close"] for b in bars], 20, t)
    if ma20 and c / ma20 - 1.0 > 0.12:
        return False
    return True


def sig_trend_pullback(bars, i):
    """形态 A：MA20 回踩低吸。"""
    closes = [b["close"] for b in bars]
    vols = [b["vol"] for b in bars]
    t = i - 1
    ma20 = sma(closes, 20, t)
    if ma20 is None or not _right_side_ok(bars, i):
        return False
    c = closes[t]
    dev = c / ma20 - 1.0
    if not (-0.02 <= dev <= 0.035):
        return False
    # 前期放量上涨：T-9 ~ T-5 区间内至少一根涨幅 ≥ 3% 且量 > 20 日均量 × 1.3
    vol20 = sma(vols, 20, t)
    if not vol20:
        return False
    impulse = False
    imp_vol = 0.0
    for j in range(t - 8, t - 3):
        if j < 1:
            continue
        chg = closes[j] / closes[j - 1] - 1.0
        if chg >= 0.03 and vols[j] > vol20 * 1.3:
            impulse = True
            imp_vol = max(imp_vol, vols[j])
    if not impulse:
        return False
    # 近 3~8 日缩量回调：回调均量 < 冲量段均量 × 0.7
    pb_vols = vols[t - 2: t + 1]
    if not pb_vols or sum(pb_vols) / len(pb_vols) >= imp_vol * 0.7:
        return False
    # 止跌 K 线：下影线 ≥ 实体 1 倍 或 小阳（涨幅 0~3%）
    b = bars[t]
    body = abs(b["close"] - b["open"])
    lower = b["open"] - b["low"] if b["close"] >= b["open"] else b["close"] - b["low"]
    chg_t = b["close"] / closes[t - 1] - 1.0
    hold_candle = (lower >= body * 1.0 and body > 0) or (0.0 <= chg_t <= 0.03)
    if not hold_candle:
        return False
    r = rsi(closes, 14, t)
    if r is not None and not (35.0 <= r <= 62.0):
        return False
    return True


def sig_vcp_breakout(bars, i, limit_pct):
    """形态 B：波动率收缩后温和放量突破。"""
    closes = [b["close"] for b in bars]
    vols = [b["vol"] for b in bars]
    t = i - 1
    a10, a40 = atr(bars, 10, t - 1), atr(bars, 40, t - 1)
    if a10 is None or a40 is None or a40 <= 0:
        return False
    if a10 / a40 >= 0.85:
        return False
    # T-1 突破：收盘 > 前 20 日最高（不含 T-1）
    box_high = max(bars[j]["high"] for j in range(t - 20, t))
    if closes[t] <= box_high:
        return False
    chg = closes[t] / closes[t - 1] - 1.0
    if not (0.025 <= chg <= 0.075):
        return False
    vol20 = sma(vols, 20, t - 1)
    if not vol20:
        return False
    vr = vols[t] / vol20
    if not (1.4 <= vr <= 3.0):
        return False
    # 温和突破：非一字、非涨停
    if chg >= limit_pct - 0.005:
        return False
    return True


def sig_stage2_start(bars, i):
    """形态 C：筑底后放量站上 MA60/MA120 + MA20 上穿。"""
    closes = [b["close"] for b in bars]
    vols = [b["vol"] for b in bars]
    t = i - 1
    ma20, ma60, ma120 = sma(closes, 20, t), sma(closes, 60, t), sma(closes, 120, t)
    if ma60 is None or ma120 is None:
        return False
    c = closes[t]
    if not (c > ma60 and c > ma120):
        return False
    # MA20 上穿：T-1 的 MA20 > MA60，T-6 的 MA20 ≤ MA60（近期金叉）
    ma20_prev = sma(closes, 20, t - 5)
    ma60_prev = sma(closes, 60, t - 5)
    if ma20 is None or ma20_prev is None or ma60_prev is None:
        return False
    if not (ma20 > ma60 and ma20_prev <= ma60_prev):
        return False
    # 长期筑底：T-120 ~ T-20 区间振幅 < 60%
    lo = min(bars[j]["low"] for j in range(t - 100, t - 20))
    hi = max(bars[j]["high"] for j in range(t - 100, t - 20))
    if lo <= 0 or hi / lo - 1.0 > 0.60:
        return False
    vol60 = sma(vols, 60, t - 5)
    if not vol60:
        return False
    recent_vol = sum(vols[t - 4: t + 1]) / 5.0
    if not (1.3 <= recent_vol / vol60 <= 2.5):
        return False
    return True


STRATEGIES = {
    "trend_pullback": sig_trend_pullback,
    "vcp_breakout": sig_vcp_breakout,
    "stage2_start": sig_stage2_start,
}


# ---------------------------------------------------------------- 数据

def fetch_bars(code_raw, count=800):
    """腾讯前复权日线（走 6h 本地缓存 + 全局限速）。count 取 800 以保证 3 年样本有 120 日预热。"""
    pure, pcode, board, limit_pct = normalize_code(code_raw)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (pcode, count)
    raw = _cached_kline("day_%s_%d" % (pcode, count), url)
    node = (raw.get("data") or {}).get(pcode) or {}
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        raise RuntimeError("日线为空: %s" % pcode)
    bars = [{"date": r[0], "open": float(r[1]), "close": float(r[2]), "high": float(r[3]), "low": float(r[4]), "vol": float(r[5])}
            for r in rows]
    return pure, pcode, board, limit_pct, bars


def is_one_word_limit_up(bar, prev_close, limit_pct):
    if prev_close <= 0:
        return False
    return abs(bar["open"] - bar["high"]) < 1e-4 and abs(bar["open"] - bar["low"]) < 1e-4 and bar["close"] >= prev_close * (1 + limit_pct) - 0.015


def is_one_word_limit_down(bar, prev_close, limit_pct):
    if prev_close <= 0:
        return False
    return abs(bar["open"] - bar["high"]) < 1e-4 and abs(bar["open"] - bar["low"]) < 1e-4 and bar["close"] <= prev_close * (1 - limit_pct) + 0.015


# ---------------------------------------------------------------- 单只回测

def backtest_one(code, strategy, bars=None, limit_pct=None, fee_buy=FEE_BUY, fee_sell=FEE_SELL, slippage=SLIPPAGE):
    """返回结构化结果 dict；bars/limit_pct 可由调用方预取以复用。"""
    if bars is None:
        _, _, _, lp, bars = fetch_bars(code)
        if limit_pct is None:
            limit_pct = lp
    if len(bars) < 200:
        return None
    sig = STRATEGIES[strategy]
    closes_all = [b["close"] for b in bars]

    trades = []
    pos = 0.0
    entry_px = 0.0
    entry_i = -1
    stop = 0.0
    highest = 0.0
    breakeven = False
    trailing = False
    below_ma20_days = 0
    missed = 0
    locked = 0

    for i in range(130, len(bars)):
        b = bars[i]
        prev_c = bars[i - 1]["close"]

        # ---------- 持仓管理（T+1）
        if pos > 0 and i > entry_i:
            held = i - entry_i
            c_t1 = bars[i - 1]["close"]
            a14 = atr(bars, 14, i - 1)
            ma20_t1 = sma(closes_all, 20, i - 1)
            ma10_t1 = sma(closes_all, 10, i - 1)

            # 更新最高收盘（用 T-1）
            if c_t1 > highest:
                highest = c_t1
            ret_t1 = c_t1 / entry_px - 1.0
            # 保本
            if not breakeven and ret_t1 >= 0.08:
                breakeven = True
                stop = max(stop, entry_px)
            # 追踪止盈启用
            if not trailing and (ret_t1 >= 0.15 or (ma20_t1 and c_t1 / ma20_t1 - 1.0 > 0.18)):
                trailing = True
            if trailing and a14:
                trail_stop = highest - 2.5 * a14
                stop = max(stop, trail_stop)
                # 收盘跌破 MA10
                if ma10_t1 and c_t1 < ma10_t1:
                    trail_stop = c_t1

            # 放量破 MA20 两日
            if ma20_t1 and c_t1 < ma20_t1:
                below_ma20_days += 1
            else:
                below_ma20_days = 0
            vol20_t1 = sma([x["vol"] for x in bars], 20, i - 2)
            big_drop = below_ma20_days >= 2

            exit_now = False
            if c_t1 <= stop:
                exit_now = True
            elif trailing and ma10_t1 and c_t1 < ma10_t1:
                exit_now = True
            elif big_drop:
                exit_now = True
            elif held >= MAX_HOLD:
                exit_now = True

            if exit_now:
                if is_one_word_limit_down(b, prev_c, limit_pct):
                    locked += 1
                else:
                    px = b["open"] * (1 - slippage)
                    net = (px * (1 - fee_sell)) / (entry_px / (1 - fee_buy)) - 1.0
                    trades.append({
                        "entry": bars[entry_i]["date"], "exit": b["date"], "hold": held,
                        "ret": round(net * 100, 2),
                        "mae": round((min(x["low"] for x in bars[entry_i:i]) / entry_px - 1) * 100, 2),
                        "mfe": round((max(x["high"] for x in bars[entry_i:i + 1]) / entry_px - 1) * 100, 2),
                    })
                    pos = 0.0
                    entry_px = 0.0
                    entry_i = -1
                    stop = highest = 0.0
                    breakeven = trailing = False
                    below_ma20_days = 0

        # ---------- 开仓
        if pos == 0 and i >= 130:
            try:
                buy = sig(bars, i, limit_pct) if strategy == "vcp_breakout" else sig(bars, i)
            except Exception:
                buy = False
            if buy:
                if is_one_word_limit_up(b, prev_c, limit_pct) or b["open"] >= prev_c * (1 + limit_pct) - 0.015:
                    missed += 1
                else:
                    px = b["open"] * (1 + slippage)
                    pos = 1.0
                    entry_px = px
                    entry_i = i
                    a14 = atr(bars, 14, i - 1) or (px * 0.02)
                    swing_low = min(x["low"] for x in bars[i - 15: i])
                    stop = max(0.0, min(px - 2.0 * a14, swing_low * 0.985))
                    highest = bars[i - 1]["close"]
                    breakeven = trailing = False
                    below_ma20_days = 0

    if not trades:
        return {"code": code, "strategy": strategy, "n_trades": 0}
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    # 连亏
    max_consec_loss, cur = 0, 0
    for r in rets:
        cur = cur + 1 if r <= 0 else 0
        max_consec_loss = max(max_consec_loss, cur)
    # 逐笔复利总收益
    compound = 1.0
    for r in rets:
        compound *= (1 + r / 100)
    total = (compound - 1) * 100
    # 持仓时间占比：策略大部分时间空仓，直接和满仓基准比不公平，记录下来用于解释
    bars_traded = len(bars) - 130
    holding_bars = sum(t["hold"] for t in trades)
    return {
        "code": code, "strategy": strategy,
        "n_trades": len(rets),
        "total_ret": round(total, 2),
        "avg_trade_ret": round(sum(rets) / len(rets), 2),
        "win_rate": round(len(wins) / len(rets) * 100, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "pl_ratio": round(avg_win / avg_loss, 2) if avg_loss > 0 else None,
        "avg_hold": round(sum(t["hold"] for t in trades) / len(trades), 1),
        "time_in_market_pct": round(holding_bars / max(1, bars_traded) * 100, 1),
        "max_consec_loss": max_consec_loss,
        "missed_limit_up": missed,
        "locked_limit_down": locked,
        "all_rets": [round(t["ret"], 2) for t in trades],  # 完整逐笔收益，供横截面统计（trades 只留最近10笔明细）
        "trades": trades[-10:],
    }


def fetch_index_bars(code="sh000906", count=800):
    """基准指数日线（默认中证800），用于计算同期基准收益。"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (code, count)
    raw = _cached_kline("idx_%s_%d" % (code, count), url)
    node = (raw.get("data") or {}).get(code) or {}
    rows = node.get("qfqday") or node.get("day") or []
    return {r[0]: float(r[2]) for r in rows}


# ---------------------------------------------------------------- 横截面样本

def load_sample(name=None, n=60, seed=42, min_mv=80, max_mv=2000):
    """横截面样本：31 个行业板块各取成交额前 10（腾讯 getBoardRankList, sort_type 仅支持 price/turnover），
    去重后筛流通市值 [min_mv, max_mv] 亿（机构能进、非巨盘、非微盘），随机抽 n 只。

    跨行业 + 成交活跃 + 中大盘，避免手挑带来的幸存者偏差。name 参数保留兼容，不再区分 hs300/zz500。
    """
    cands = []
    try:
        boards = json.loads(_throttled_get("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type=hy&sort_type=price&direct=down&offset=0&count=40"))
        bl = ((boards.get("data") or {}).get("rank_list")) or []
        seen = set()
        for bk in bl:
            try:
                r = json.loads(_throttled_get("https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?board_code=%s&sort_type=turnover&direct=down&offset=0&count=10" % bk["code"]))
                for x in ((r.get("data") or {}).get("rank_list") or []):
                    c = str(x.get("code") or "")
                    if len(c) == 8 and c[:2] in ("sh", "sz"):
                        pure = c[2:]
                        if pure.isdigit() and pure[:2] in ("60", "00", "30") and pure not in seen:
                            seen.add(pure)
                            mv = float(x.get("ltsz") or 0)
                            if min_mv <= mv <= max_mv:
                                cands.append(pure)
            except Exception:
                continue
    except Exception:
        pass
    if not cands:
        raise SystemExit("无法构建横截面样本：腾讯板块接口不可用")
    random.Random(seed).shuffle(cands)
    return cands[:n]


def run_cross_section(sample, strategy, n, workers=6, preloaded=None):
    """preloaded: {code: (limit_pct, bars)} 预加载的日线，多策略复用时避免重复请求。"""
    codes = load_sample(sample, n) if preloaded is None else list(preloaded.keys())
    out = []

    def work(c):
        try:
            if preloaded is not None:
                lp, bars = preloaded[c]
            else:
                _, _, _, lp, bars = fetch_bars(c)
            return backtest_one(c, strategy, bars=bars, limit_pct=lp)
        except Exception as e:
            return {"code": c, "strategy": strategy, "error": str(e)[:60], "n_trades": 0}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        out = list(ex.map(work, codes))
    return out


def preload_bars(sample, n, workers=6):
    """一次性拉取样本内所有日线，供多策略复用。

    遇到 WAF 拦截立即中止（fail-fast），避免 120 只全部跑成空结果还误以为「无信号」。
    """
    codes = load_sample(sample, n)
    waf_hit = threading.Event()
    def work(c):
        if waf_hit.is_set():
            return c, None
        try:
            _, _, _, lp, bars = fetch_bars(c)
            return c, (lp, bars)
        except RuntimeError as e:
            if "WAF" in str(e):
                waf_hit.set()
            return c, None
        except Exception:
            return c, None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pairs = list(ex.map(work, codes))
    loaded = {c: v for c, v in pairs if v is not None}
    if waf_hit.is_set() and len(loaded) < len(codes) * 0.5:
        raise SystemExit("腾讯 WAF 拦截，仅加载 %d/%d 只，中止回测。请等待 10~30 分钟后重试（已加载的 %d 只已进本地缓存）。"
                         % (len(loaded), len(codes), len(loaded)))
    return loaded


def summarize(results, strategy, index_close=None):
    """汇总。基准用中证800 指数（而非个股买入持有——后者在「今日活跃股」样本上有严重前视偏差）。"""
    valid = [r for r in results if r and r.get("n_trades", 0) > 0]
    errs = [r for r in results if r and r.get("error")]
    if not valid:
        return {"strategy": strategy, "n_codes": len(results), "n_valid": 0, "n_errors": len(errs)}

    def avg(k):
        vals = [r[k] for r in valid if r.get(k) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    # 每笔交易相对指数同期的超额（用完整逐笔收益 all_rets，trades 字段只留最近 10 笔明细）
    trade_rets = []
    for r in valid:
        trade_rets.extend(r.get("all_rets") or [t["ret"] for t in r["trades"]])
    n_tr = len(trade_rets)
    pos = sum(1 for x in trade_rets if x > 0)
    # 单侧符号检验：单笔收益 > 0 的比例是否显著 > 50%（注意：波段高赔率低胜率是常态，此检验仅作参考）
    z = (pos - 0.5 * n_tr) / math.sqrt(0.25 * n_tr) if n_tr else 0.0
    p = 1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    # 单笔 t 检验：均值是否显著 > 0
    if n_tr > 2:
        m = sum(trade_rets) / n_tr
        sd = math.sqrt(sum((x - m) ** 2 for x in trade_rets) / (n_tr - 1))
        t_stat = m / (sd / math.sqrt(n_tr)) if sd > 1e-9 else 0.0
        # 双侧 p 近似（正态）
        t_p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t_stat) / math.sqrt(2.0))))
    else:
        t_stat, t_p = 0.0, 1.0

    return {
        "strategy": strategy,
        "n_codes": len(results), "n_valid": len(valid), "n_errors": len(errs),
        "avg_total_ret_per_stock": avg("total_ret"),
        "avg_time_in_market_pct": avg("time_in_market_pct"),
        "n_trades_total": n_tr,
        "avg_trades_per_stock": round(n_tr / len(valid), 1),
        "trade_win_rate": round(pos / n_tr * 100, 1) if n_tr else None,
        "avg_trade_ret": round(sum(trade_rets) / n_tr, 2) if n_tr else None,
        "median_trade_ret": round(sorted(trade_rets)[n_tr // 2], 2) if n_tr else None,
        "avg_win": avg("avg_win"), "avg_loss": avg("avg_loss"),
        "avg_pl_ratio": avg("pl_ratio"),
        "avg_hold_days": avg("avg_hold"),
        "t_stat": round(t_stat, 2), "t_p_two_sided": round(t_p, 3),
        "sign_test_p": round(p, 3),
        "max_consec_loss_worst": max(r["max_consec_loss"] for r in valid),
        "total_missed_limit_up": sum(r["missed_limit_up"] for r in valid),
        "total_locked_limit_down": sum(r["locked_limit_down"] for r in valid),
    }


def main():
    ap = argparse.ArgumentParser(description="趋势波段策略回测（S1 验证）")
    ap.add_argument("--code", default=None, help="单只回测")
    ap.add_argument("--sample", default="cross", help="横截面样本名（默认 cross=跨行业中大盘）")
    ap.add_argument("--strategy", default="trend_pullback", choices=list(STRATEGIES) + ["all"])
    ap.add_argument("--n", type=int, default=60, help="横截面样本数")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default=None, help="横截面结果 JSON 输出路径")
    args = ap.parse_args()

    if args.code:
        for s in (STRATEGIES if args.strategy == "all" else [args.strategy]):
            try:
                r = backtest_one(args.code, s)
            except RuntimeError as e:
                raise SystemExit(str(e))
            except json.JSONDecodeError:
                raise SystemExit("腾讯接口返回非 JSON（可能被 WAF 拦截），请稍后重试")
            print(json.dumps(r, ensure_ascii=False, indent=2) if r else "%s: 数据不足" % s)
        return

    if not args.sample:
        ap.error("需要 --code 或 --sample")

    strategies = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    # 日线只拉一次，三策略复用；指数基准单独记录（个股买入持有在「今日活跃股」样本上有前视偏差，不作基准）
    print(">>> 预加载 %d 只日线 ..." % args.n, file=sys.stderr)
    preloaded = preload_bars(args.sample, args.n, args.workers)
    try:
        idx = fetch_index_bars()
        idx_keys = sorted(idx.keys())
        index_bench = round((idx[idx_keys[-1]] / idx[idx_keys[-750]] - 1) * 100, 2) if len(idx_keys) > 750 else None
    except Exception:
        index_bench = None
    report = {
        "sample": args.sample, "n_requested": args.n, "n_loaded": len(preloaded),
        "index_bench_zz800_3y_pct": index_bench,
        "note": "基准为中证800指数近3年；个股买入持有因样本按今日成交额选取存在前视偏差，不作基准。单笔收益已扣手续费/印花税/滑点。",
        "strategies": {},
    }
    for s in strategies:
        print(">>> 回测 %s ..." % s, file=sys.stderr)
        res = run_cross_section(args.sample, s, args.n, args.workers, preloaded=preloaded)
        report["strategies"][s] = {"summary": summarize(res, s), "detail": res}
        print(json.dumps(report["strategies"][s]["summary"], ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("已写入 %s" % args.out, file=sys.stderr)


if __name__ == "__main__":
    main()
