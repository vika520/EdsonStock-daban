#!/usr/bin/env python3
"""趋势波段 · 价值动量 主选股引擎（零依赖 Python 3.8+）— 对应 references/swing_design.md 的 S3。

五层漏斗（与打板池 auto_pick_top10.py 完全独立）：
  1) 市场择时（0~100 分 → 进攻/震荡/防守），驱动推送数量与仓位建议
  2) 行业轮动（sector_rotation.py）：主升期/启动期板块为主力池
  3) 基本面排雷（fetch_fundamentals.py）：硬否决 + VQ 打分
  4) 技术形态（swing_backtest.py 的信号函数，零未来函数）+ 禁追高
  5) 交易计划：买点区间 / ATR 止损 / 保本 / 追踪止盈 + 追踪账本

输出 web/swing_picks.json（契约见 swing_design.md 第 2 节）。
数据不可用时（如腾讯 WAF 拦截）输出 ok=false 并保留旧文件，不用空数据覆盖（与打板池同一纪律）。

用法：
  python3 scripts/auto_pick_swing.py --mode post --out web/swing_picks.json
"""
import argparse
import datetime as dt
import json
import pathlib
import sys

from realtime_and_short import http_get, normalize_code

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from sector_rotation import build_rotation, now_beijing as _sr_now
from fetch_fundamentals import fetch_financials, fetch_predict, fetch_unlock_map, hard_veto, value_quality_score
from swing_backtest import fetch_bars, sig_trend_pullback, sig_vcp_breakout, sig_stage2_start, atr, sma, rsi, _no_chase
from ashare_backtest import is_limit_up_close

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
MAX_PICKS = {"进攻期": 8, "震荡轮动期": 5, "防守避险期": 0}
MAX_CAND_PER_SECTOR = 6     # 每板块取成分股上限（控制请求量）
VQ_MIN = 60.0               # VQ 打分下限


# ---------------------------------------------------------------- 第 1 层：市场择时

def fetch_index_ma_status(count=70):
    """三大指数 vs MA20/MA60 + MA20 斜率。返回 (score_0_40, 描述, n_ok)。

    数据缺失的指数不计入分母（而不是按 0 分计）——拉取失败把趋势分压成 0 会误判防守期，
    与打板池「空池不判冰点」同一原则。
    """
    total, desc, ok_n = 0.0, [], 0
    for code, name in [("sh000300", "沪深300"), ("sh000905", "中证500"), ("sz399006", "创业板指")]:
        try:
            raw = json.loads(http_get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (code, count)))
            node = (raw.get("data") or {}).get(code) or {}
            rows = node.get("qfqday") or node.get("day") or []
            if len(rows) < 65:
                desc.append("%s:数据不足" % name)
                continue
            c = [float(r[2]) for r in rows]
            last = c[-1]
            ma20 = sum(c[-20:]) / 20
            ma60 = sum(c[-60:]) / 60
            ma20_slope = (ma20 - sum(c[-25:-5]) / 20) / 5
            s = 0.0
            if last > ma20: s += 0.6
            if last > ma60: s += 0.4
            if ma20_slope > 0: s += 0.5
            if ma20 > ma60: s += 0.5
            total += min(2.0, s)
            ok_n += 1
            desc.append("%s%s%s" % (name, "站上MA20" if last > ma20 else "破MA20", "/MA20上行" if ma20_slope > 0 else "/MA20走平"))
        except Exception:
            desc.append("%s:拉取失败" % name)
    # 只有成功拉到的指数参与折算；全部失败返回 None 表示该维度不可用
    if ok_n == 0:
        return None, "；".join(desc), 0
    return round(total / (ok_n * 2.0) * 40.0, 1), "；".join(desc), ok_n


def fetch_breadth_score():
    """市场宽度（30 分）：flow.json 的 NH-NL 5 日均值 + 涨跌家数比。【量价代理】

    注意：nh60/nl60 是「当日」新高新低家数绝对值（非差分序列），直接取最近 5 行求均值即可；
    不要取 6 行再丢首行——数据不足 6 行时会除以 5 但只有 4 个样本，系统性缩小 20%。
    """
    try:
        from query import load
        d, _ = load("flow", True)
        rows = (d.get("rows") or [])[-5:]
        if len(rows) < 5:
            return 15.0, "宽度数据不足(中性)"
        # 字段序: date,up,dn,fl,n,uv,dv,nh60,nl60,nh250,nl250,...
        avg_nn60 = sum(int(r[7]) - int(r[8]) for r in rows) / len(rows)
        avg_nn250 = sum(int(r[9]) - int(r[10]) for r in rows) / len(rows)
        avg_upr = sum(int(r[1]) / max(1, int(r[1]) + int(r[2])) for r in rows) / len(rows)
        s = 0.0
        if avg_nn60 > 0: s += 1.0
        if avg_nn250 > 0: s += 0.5
        s += min(1.5, max(0.0, (avg_upr - 0.35) / 0.2) * 1.5)   # 上涨占比 35%→55% 映射 0→1.5
        return round(s / 3.0 * 30.0, 1), "NH60-NL60 5日均值 %+.0f、NH250-NL250 %+.0f、上涨占比 %.0f%% [量价代理]" % (avg_nn60, avg_nn250, avg_upr * 100)
    except Exception as e:
        return 15.0, "宽度数据拉取失败(%s)，中性给分" % str(e)[:30]


def fetch_deriv_leverage_score():
    """衍生品与杠杆（20 分）。【真实公开数据】

    leverage.rows 列结构（fetch_extras.py）：
      [date, RZYE(融资余额), RZMRE, RZJME, RQYE, RZRQYE(两融余额), RZYEZB, LTSZ(流通市值)]
    —— 必须读 r[5]（RZRQYE）。曾误用 r[-1] 把流通市值当两融余额，导致大盘下跌被重复扣分。
    derivatives.futures.<品种> 行结构：[..., basis, annNear(年化基差%), ...]，取 IM/IC 当月年化基差。
    """
    try:
        from query import load
        d, _ = load("sentiment", True)
        s, notes = 10.0, []
        # ---- 两融余额 5 日变化（RZRQYE = r[5]）
        lv = (d.get("leverage") or {})
        rows = lv.get("rows") or []
        if len(rows) >= 6:
            try:
                bal = [float(r[5]) for r in rows[-6:]]     # RZRQYE 两融余额
                chg5 = (bal[-1] / bal[0] - 1) * 100 if bal[0] > 0 else 0
                if chg5 > 0.5: s += 5
                elif chg5 < -1.0: s -= 5
                notes.append("两融余额5日%+.2f%%" % chg5)
            except Exception:
                notes.append("两融余额列异常")
        else:
            notes.append("两融数据不足")
        # ---- IM/IC 当月年化基差：深度贴水（<-15%）= 机构集中对冲，扣 4 分
        dv = d.get("derivatives") or {}
        ann_list = []
        for code in ("IM", "IC"):
            fr = (dv.get("futures") or {}).get(code) or []
            if len(fr) >= 2:
                try:
                    ann_list.append(float(fr[-1][4]))   # annNear 年化基差
                except Exception:
                    pass
        if ann_list:
            ann = sum(ann_list) / len(ann_list)
            if ann < -15.0:
                s -= 4
                notes.append("IM/IC年化贴水%.1f%%(深贴水对冲)" % ann)
            elif ann > 0:
                s += 2
                notes.append("IM/IC年化基差%+.1f%%" % ann)
            else:
                notes.append("IM/IC年化基差%+.1f%%" % ann)
        return max(0.0, min(20.0, s)), "；".join(notes) + " [真实公开数据]"
    except Exception:
        return 10.0, "衍生品/两融拉取失败，中性给分"


def build_regime():
    """第 1 层：择时总分与三档状态。

    数据缺失的维度按「中性 50%」给分并在 factors 中标注，绝不按 0 分计——
    否则一次网络故障就会把系统打到防守期停发所有信号。
    """
    t_score, t_desc, t_ok = fetch_index_ma_status()
    if t_score is None:
        t_score, t_desc = 20.0, t_desc + "（趋势维度不可用，中性 50% 给分）"
    b_score, b_desc = fetch_breadth_score()
    d_score, d_desc = fetch_deriv_leverage_score()
    erp_note = ""
    try:
        from query import load
        e, _ = load("erp", True)
        snap = (e.get("snapshot") or {})
        erp_note = "ERP %.2f%%（%.0f%%分位，仅作仓位上限锚）" % (snap.get("erp") or 0, snap.get("historyPercentile") or 0)
    except Exception:
        erp_note = "ERP 拉取失败"
    score = round(t_score + b_score + d_score, 0)
    if score >= 65:
        phase, pos = "进攻期", "60%~80%"
    elif score >= 40:
        phase, pos = "震荡轮动期", "30%~50%"
    else:
        phase, pos = "防守避险期", "0%~20%"
    return {
        "score": int(score), "phase": phase, "suggestedPos": pos,
        "factors": {"indexTrend": t_desc, "breadth": b_desc, "derivatives": d_desc, "erpBackground": erp_note},
    }


# ---------------------------------------------------------------- 第 2~5 层

def fetch_sector_constituents(board_code, max_n=MAX_CAND_PER_SECTOR):
    """板块成分股（成交额降序），返回 [{code,name,pe,ltsz}]。"""
    url = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?board_code=%s&sort_type=turnover&direct=down&offset=0&count=%d"
           % (board_code, max_n))
    out = []
    try:
        raw = json.loads(http_get(url))
        for x in ((raw.get("data") or {}).get("rank_list")) or []:
            c = str(x.get("code") or "")
            if len(c) == 8 and c[:2] in ("sh", "sz"):
                pure = c[2:]
                if pure.isdigit() and pure[:2] in ("60", "00", "30"):
                    out.append({"code": pure, "name": x.get("name"),
                                "pe": float(x["pe_ttm"]) if x.get("pe_ttm") and str(x["pe_ttm"]).replace('.', '').replace('-', '').isdigit() else None,
                                "circMv": float(x.get("ltsz") or 0)})
    except Exception:
        pass
    return out


def classify_setup(bars, i, limit_pct):
    """对最新交易日（今日收盘，盘后口径）判定形态类型。

    审计修正（2026-11-25）：
    1) 只判今日 i——引擎只在盘后跑（15:20/18:00），今日数据已完整；
       原先「先试昨日 i-1 再试今日」会让昨日信号拉进今日池子，而交易计划却按今日算，时序错位。
    2) 形态判定前强制 _no_chase()——当日涨停/涨幅>7%/偏离MA20>12% 一律不录取。
    3) vcp_breakout 在 S1 回测中不显著（p=0.122），降级为「观察」标记，不入正式推荐。
    """
    if not _no_chase(bars, i, limit_pct):
        return None
    if sig_trend_pullback(bars, i):
        return "MA20_PULLBACK", "趋势回踩MA20低吸"
    if sig_stage2_start(bars, i):
        return "STAGE2_START", "底部反转右侧启动"
    if sig_vcp_breakout(bars, i, limit_pct):
        return "OBSERVE_VCP", "波动收缩突破(观察)"
    return None


def _stock_meta_from_bars(bars, limit_pct):
    """从日线提取 hard_veto 需要的补充字段：20日均成交额(亿)、上市天数、近15日连板数。"""
    n = len(bars)
    if n < 25:
        return {"amount20Yi": None, "listedDays": None, "recentBoards": None}
    amt20 = sum(b["close"] * b["vol"] * 100 for b in bars[-20:]) / 20 / 1e8
    listed_days = None   # 腾讯日线不提供上市日；仅当日线根数 < 180 时标记次新
    boards = 0
    for k in range(max(1, n - 15), n):
        if is_limit_up_close(bars[k], bars[k - 1]["close"], limit_pct):
            boards += 1
    return {
        "amount20Yi": round(amt20, 2),
        "listedDays": n if n < 200 else 999,   # 日线不足 200 根视为上市约 <1 年
        "recentBoards": boards,
    }


def build_trade_plan(bars, i, entry_px, limit_pct):
    """第 5 层：交易计划。entry_px = T 日开盘（含滑点近似用开盘价）。"""
    closes = [b["close"] for b in bars]
    t = i - 1
    ma20 = sma(closes, 20, t)
    a14 = atr(bars, 14, t) or entry_px * 0.02
    swing_low = min(x["low"] for x in bars[i - 15:i])
    stop = max(0.0, min(entry_px - 2.0 * a14, swing_low * 0.985))
    return {
        "refPrice": round(entry_px, 2),
        "ma20": round(ma20, 2) if ma20 else None,
        "atr14": round(a14, 2),
        "buyZone": [round(max(0.01, (ma20 or entry_px) * 0.99), 2), round((ma20 or entry_px) * 1.03, 2)],
        "stopLoss": round(stop, 2),
        "stopPct": round((stop / entry_px - 1) * 100, 1),
        "breakevenTrigger": round(entry_px * 1.08, 2),
        "trailingRule": "浮盈>=15%后收盘破MA10或回撤2.5*ATR止盈",
        "holdHorizon": "5~20 个交易日",
        "riskPosSuggestion": "单票参考 15% (1.5%净值风控)",
    }


def generate_swing_picks(mode="post", top_sectors=5):
    """主入口：mode 仅影响输出标注（波段只做盘后，15:20 初选 / 18:00 终选）。"""
    now_bj = _sr_now()
    date_str = now_bj.strftime("%Y-%m-%d")

    # ---- 第 1 层 择时
    regime = build_regime()

    out = {
        "ok": True,
        "tradeDate": date_str,
        "generatedAt": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "generatedAtTz": "Asia/Shanghai (UTC+8)",
        "mode": mode,
        "regime": regime,
    }

    # 防守期：picks 为空但 regime / sectorRotation 必须有效
    rotation = build_rotation("hy", top=top_sectors)
    out["sectorRotation"] = rotation.get("boards") or []
    out["sectorMeta"] = {k: rotation.get(k) for k in ("nBoards", "nRaw", "stageCount", "bench", "dataTierNote")}
    if regime["phase"] == "防守避险期":
        out["picks"] = []
        out["note"] = "防守避险期：暂停进攻型波段买入信号；可关注高股息防御（本引擎不输出）。"
        return out

    max_picks = MAX_PICKS[regime["phase"]]
    # ---- 第 2~4 层：轮动板块 → 成分股排雷打分 → 形态
    # 审计修正（2026-11-25）：
    #  a) 板块扩围：hy top12 + gn top10 全部参与（原先 hy 只取 5 再筛阶段，启动期板块若总分排第 6 直接丢失）
    #  b) 去掉循环内 break：全量打分排序后截断，高分板块的候补有机会参与全局排序
    #  c) 日线先拉再 veto：hard_veto 需要 20 日均成交额/上市天数/近 15 日连板数（原先未传，成交额否决形同虚设）
    unlock = fetch_unlock_map()
    hy = build_rotation("hy", top=12)
    gn = build_rotation("gn", top=10)
    all_boards = ((hy.get("boards") or []) if hy.get("ok") else []) + ((gn.get("boards") or []) if gn.get("ok") else [])
    boards = [b for b in all_boards if b["stage"] in ("主升期", "启动期")]
    picks, errors = [], []
    funnel = {"sectors": len(boards), "candidates": 0, "vetoed": 0, "lowVQ": 0, "noBars": 0, "noSetup": 0, "chased": 0, "picked": 0, "observed": 0}
    veto_examples = []
    for bd in boards:
        cons = fetch_sector_constituents(bd["code"])
        for cs in cons:
            code = cs["code"]
            funnel["candidates"] += 1
            try:
                # 1) 日线先拉：veto 与形态都需要它
                _, _, _, limit_pct, bars = fetch_bars(code, 260)
                if len(bars) < 150:
                    funnel["noBars"] += 1
                    continue
                meta = _stock_meta_from_bars(bars, limit_pct)
                # 2) 排雷：补齐 amount20Yi / listedDays / 连板数（原先缺参导致成交额否决失效）
                fin = fetch_financials(code)
                pred = fetch_predict(code)
                vetoed, reasons = hard_veto(code, fin, pred, unlock,
                                             {"circMv": cs["circMv"], "amount20Yi": meta["amount20Yi"]},
                                             listed_days=meta["listedDays"], recent_boards=meta["recentBoards"])
                if vetoed:
                    funnel["vetoed"] += 1
                    if len(veto_examples) < 5:
                        veto_examples.append({"code": code, "reasons": reasons})
                    continue
                vq, vq_parts, vq_notes = value_quality_score(fin, pred, pe_ttm=cs["pe"])
                if vq < VQ_MIN:
                    funnel["lowVQ"] += 1
                    continue
                # 3) 形态：只判今日收盘（盘后口径），内部强制 _no_chase
                i = len(bars)
                setup = classify_setup(bars, i, limit_pct)
                if not setup:
                    funnel["noSetup"] += 1
                    continue
                setup_type, setup_label = setup
                # vcp_breakout 在 S1 不显著（p=0.122）——只作观察标记，不入正式推荐
                is_observe = setup_type.startswith("OBSERVE_")
                if is_observe:
                    funnel["observed"] += 1
                b_today = bars[-1]
                entry_px = b_today["close"] * 1.001   # 盘后口径：以收盘价为参考入场价
                plan = build_trade_plan(bars, i, entry_px, limit_pct)
                picks.append({
                    "code": code, "name": cs["name"], "sector": bd["name"], "sectorStage": bd["stage"],
                    "setupType": setup_type, "setupLabel": setup_label,
                    "observe": is_observe,
                    "totalScore": round(bd["totalScore"] * 0.4 + vq * 0.6, 1),
                    "scores": {"sector": bd["totalScore"], "fundamental": vq, "vqParts": vq_parts},
                    "fundamentals": {
                        "circMvYi": cs["circMv"], "peTtm": cs["pe"], "amt20Yi": meta["amount20Yi"],
                        "roeTtmEst": fin.get("roeTtmEst"), "revYoy": fin.get("revYoy"), "npYoy": fin.get("npYoy"),
                        "predictTag": (pred.get("predictType") and "%s [公告口径]" % pred["predictType"]) or None,
                        "unlockSafe": code not in unlock or unlock[code]["ratio"] <= 3.0,
                    },
                    "vqNotes": vq_notes,
                    "tradePlan": plan,
                    "dataTierNote": "财务/K线/预告/解禁为真实公开数据；ROE 为估算；板块拥挤度为量价代理",
                })
            except RuntimeError as e:
                errors.append({"code": code, "error": str(e)[:80]})
                if "WAF" in str(e):
                    # WAF 拦截：立即中止，输出 ok=false（调用方保留旧文件）
                    return {"ok": False, "error": "腾讯 WAF 拦截：%s" % str(e)[:120], "tradeDate": date_str, "generatedAt": now_bj.strftime("%Y-%m-%d %H:%M:%S")}
            except Exception as e:
                errors.append({"code": code, "error": str(e)[:60]})
    # 全量排序后截断：正式推荐取非观察的前 max_picks；观察标记附在末尾（不占正式名额）
    picks.sort(key=lambda p: -p["totalScore"])
    formal = [p for p in picks if not p.get("observe")]
    observed = [p for p in picks if p.get("observe")][:3]
    out["picks"] = formal[:max_picks]
    out["observedPicks"] = observed
    funnel["picked"] = len(out["picks"])
    funnel["observedPicked"] = len(observed)
    out["funnel"] = funnel
    out["vetoExamples"] = veto_examples
    out["candidateErrors"] = errors[:10]
    out["note"] = "%s：推 %d/%d 只（上限 %d）%s" % (regime["phase"], len(out["picks"]), len(picks), max_picks,
                                                  "，另有 %d 只观察标记" % len(observed) if observed else "")
    return out


# ---------------------------------------------------------------- 账本

TRACKER_FILE = BASE_DIR / "web" / "swing_tracker.json"
MAX_TRACK_DAYS = 20


def _tracker_refresh_prices(tracker):
    """拉取账本内活跃标的的最新收盘价，刷新 lastRet 与退出状态。

    状态机：已止损(收盘<=stopLoss) > 已触发保本(收盘>=breakevenTrigger) > 超时离场(daysHeld>=20) > 持仓中。
    已终结状态（已止损/超时离场）不再刷新。
    """
    active = [t for t in tracker if t.get("status") not in ("已止损", "超时离场")]
    if not active:
        return
    # 腾讯 L1 快照取最新价（含已收盘场景的收盘价）
    try:
        from auto_pick_top10 import fetch_l1_map
        l1 = fetch_l1_map([t["code"] for t in active])
    except Exception:
        l1 = {}
    for t in active:
        q = l1.get(t["code"])
        if not q or not q.get("price"):
            continue
        px = q["price"]
        t["lastPx"] = round(px, 2)
        if t.get("entryRef"):
            t["lastRet"] = round((px / t["entryRef"] - 1) * 100, 2)
        stop = t.get("stopLoss") or 0
        be = t.get("breakevenTrigger") or 0
        if stop and px <= stop:
            t["status"] = "已止损"
        elif be and px >= be:
            t["status"] = "已触发保本"
        elif (t.get("daysHeld") or 0) >= MAX_TRACK_DAYS:
            t["status"] = "超时离场"
        else:
            t["status"] = "持仓中"


def update_tracker(result):
    """更新追踪账本。

    幂等修正（2026-11-25 审计）：原先同一天重跑 N 次 daysHeld 就 +N（15:20 初选、18:00 终选、
    手动刷新一天可跑 3 次）。现用 lastUpdatedDate 锁——每个标的一天只累计一次。
    """
    tracker = []
    try:
        tracker = json.loads(TRACKER_FILE.read_text(encoding="utf-8"))
    except Exception:
        tracker = []
    today = result.get("tradeDate")
    by_code = {t.get("code"): t for t in tracker if t.get("code")}
    # 幂等刷新旧条目：每天首次运行才 +1 天
    for t in tracker:
        if t.get("status") in ("已止损", "超时离场"):
            continue
        if t.get("lastUpdatedDate") != today:
            t["daysHeld"] = (t.get("daysHeld") or 0) + 1
            t["lastUpdatedDate"] = today
    # 合并今日新入选
    for p in result.get("picks") or []:
        old = by_code.get(p["code"])
        if old and old.get("entryDate") == today:
            old["stopLoss"] = p["tradePlan"]["stopLoss"]
            old["breakevenTrigger"] = p["tradePlan"]["breakevenTrigger"]
            old["lastUpdatedDate"] = today
            continue
        tracker.append({
            "code": p["code"], "name": p["name"], "sector": p["sector"],
            "entryDate": today, "entryRef": p["tradePlan"]["refPrice"],
            "stopLoss": p["tradePlan"]["stopLoss"], "breakevenTrigger": p["tradePlan"]["breakevenTrigger"],
            "setupType": p["setupType"], "status": "观察中",
            "daysHeld": 0, "lastRet": None, "lastUpdatedDate": today,
        })
    # 刷新价格与状态
    _tracker_refresh_prices(tracker)
    # 只保留最近 120 条
    tracker = tracker[-120:]
    try:
        TRACKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRACKER_FILE.write_text(json.dumps(tracker, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("[Tracker] 写入失败: %s" % e, file=sys.stderr)
    return tracker


def main():
    ap = argparse.ArgumentParser(description="趋势波段 · 价值动量 主选股引擎")
    ap.add_argument("--mode", default="post", choices=["post"], help="波段只做盘后（15:20 初选 / 18:00 终选共用 post）")
    ap.add_argument("--top-sectors", type=int, default=5)
    ap.add_argument("--out", default=None, help="输出路径（默认 web/swing_picks.json）")
    args = ap.parse_args()

    out_path = pathlib.Path(args.out) if args.out else BASE_DIR / "web" / "swing_picks.json"
    result = generate_swing_picks(args.mode, args.top_sectors)

    if not result.get("ok"):
        print("[SwingPicker] 生成失败：%s，保留原文件" % result.get("error"), file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    tracker = update_tracker(result)
    result["activeTracker"] = tracker
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    r = result["regime"]
    print("=== 趋势波段选股完成 (%s | %s) ===" % (result["tradeDate"], r["phase"]))
    print("  择时: %d 分 → %s，建议仓位 %s" % (r["score"], r["phase"], r["suggestedPos"]))
    print("  因子: 趋势[%s] 宽度[%s]" % (r["factors"]["indexTrend"], r["factors"]["breadth"]))
    print("  已写入 %s（%d 只标的，账本 %d 条）" % (out_path, len(result.get("picks") or []), len(tracker)))
    for i, p in enumerate(result.get("picks") or [], 1):
        tp = p["tradePlan"]
        print("  %d. [%s] %s(%s | %s) 总分%.1f | 买点%.2f~%.2f 止损%.2f(%.1f%%)" % (
            i, p["setupLabel"], p["name"], p["code"], p["sector"], p["totalScore"],
            tp["buyZone"][0], tp["buyZone"][1], tp["stopLoss"], tp["stopPct"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
