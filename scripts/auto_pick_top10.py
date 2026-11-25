#!/usr/bin/env python3
"""EdsonStock × A股超短游资 Top 10 量化选股引擎 (auto_pick_top10.py)。

四维游资量化矩阵 + 情绪周期动态配额 + 封流比联合评分 + 龙虎榜负面席位否决：
  - 冰点/退潮期（涨停<35 或 跌停>15）：>=4 板高标配额归零，改选 1进2 首板与首阴回踩 5 日线；
  - 主升期：扩大主线 1进2 / 2进3 配额；
  - 评分：封成比 + 封流比(封单/流通市值)*8 + 黄金换手 8%~28% 加 15 分；
  - 龙虎榜：买榜 >=3 拉萨散户席位且卖榜净卖出 -> 一票否决；量化做T扣 30 分；买一占比 >15% 独食板预警；
  - 9:25 竞价模式：竞价成交额/昨日全天成交额(量价代理) 5%~18% + 高开 1.0%~7.5% 才标记【9:25真弱转强】并置顶。

口径说明：edsonstock-data 的 sentiment 快照为盘后公开数据；昨日全天成交额由腾讯日K成交量×均价推算，属量价代理，不是真实成交额。
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from query import load as edson_load
from realtime_and_short import fetch_em_pool, http_get, normalize_code, tag_seat

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def fmt_time(fbt_raw):
    s = str(fbt_raw or "").strip().zfill(6)
    if len(s) == 6 and s.isdigit():
        return "%s:%s:%s" % (s[:2], s[2:4], s[4:6])
    return s or "--:--:--"


def load_sentiment_snapshot():
    """读取 edsonstock-data 的 sentiment 快照（盘后公开数据），失败时返回空。"""
    try:
        data, src = edson_load("sentiment", True)
        snap = ((data or {}).get("sentiment") or {}).get("snapshot") or {}
        return snap, src
    except Exception:
        return {}, "unavailable"


def fetch_l1_map(codes):
    """批量拉取腾讯 L1 快照，返回 {纯代码: {circMv, turnover, amountYi, ...}}。"""
    if not codes:
        return {}
    norm_map = {}
    for c in codes:
        try:
            pure, pcode, _, _ = normalize_code(c)
            norm_map[pcode] = pure
        except Exception:
            pass
    if not norm_map:
        return {}
    try:
        text = http_get("https://qt.gtimg.cn/q=" + ",".join(norm_map.keys()), encoding="gbk")
    except Exception:
        return {}
    out = {}
    for line in text.strip().splitlines():
        if "~" not in line:
            continue
        p = line.split("~")
        if len(p) < 50:
            continue
        out[p[2]] = {
            "name": p[1] or "",
            "price": _num(p[3]),
            "preClose": _num(p[4]),
            "open": _num(p[5]),
            "amountYi": round(_num(p[37]) / 10000.0, 3),
            "turnover": _num(p[38]),
            "circMv": _num(p[44]),
            "volRatio": _num(p[49]),
        }
    return out


def now_beijing():
    """统一北京时间（UTC+8），不依赖运行环境 TZ。"""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=8)


def fetch_kline_rows(code_raw, count=7):
    """腾讯日K：返回行 [date, open, close, high, low, volume(手)]。"""
    try:
        _, pcode, _, _ = normalize_code(code_raw)
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (pcode, count)
        raw = json.loads(http_get(url))
        node = (raw.get("data") or {}).get(pcode) or {}
        return node.get("qfqday") or node.get("day") or []
    except Exception:
        return []


def _row_amount_est_yi(r):
    """单根日K成交额估算（量价代理）：成交量(手)×100×OHLC均价 / 1e8。"""
    vol = _num(r[5])
    o, c, h, l = _num(r[1]), _num(r[2]), _num(r[3]), _num(r[4])
    return round(vol * 100.0 * ((o + c + h + l) / 4.0) / 1e8, 3)


def _latest_complete_bars(rows, today):
    """返回「最新已完成交易日」及之前的 K 线切片。

    2026-11-25 审计修复（跨日时序错位）：原逻辑 `r[0] < today` 永远排除当日行——
    盘后运行时 11-25 当日 K 线已存在（阳线涨停），却被丢弃，拿 11-24 的阴线当「最新」，
    结果把「今天已反包涨停」的票标成「今日首阴回踩」推荐明天低吸。
    正确逻辑：当日行存在 → 当日就是最新完成日；当日行不存在（盘中/数据未插入）→ 退回最后一行。
    另外两条纪律：① 候选池是「今日涨停」的票，今天涨停就不可能是首阴，必须在调用处排除；
                 ② 首阴回踩买点是「T 日阴线 → T+1 低吸」，盘后选股 T 日就是今天。
    """
    dated = [(str(r[0]), r) for r in rows]
    has_today = any(d == today for d, _ in dated)
    if has_today:
        return [r for d, r in dated if d <= today]
    return list(rows)


def fetch_prev_day_amount_est_yi(code_raw, today=None):
    """昨日(T-1)全天成交额估算。

    取「严格早于今日」的最后一行。注意：这个函数的语义就是 T-1，所以这里用 < today 是对的
    （竞价模式算量比要拿昨日全天成交额，今天的成交额还没走完）。
    """
    today = today or now_beijing().strftime("%Y-%m-%d")
    rows = fetch_kline_rows(code_raw, 6)
    prev_rows = [r for r in rows if str(r[0]) < today]
    if not prev_rows:
        return 0.0
    return _row_amount_est_yi(prev_rows[-1])


def _limit_pct_for(code):
    """按代码推断涨跌停幅度：双创 20%，北交所 30%，主板 10%。"""
    try:
        return normalize_code(code)[3]
    except Exception:
        return 0.10


def fetch_first_yin_ma5_candidates(codes, limit=2, today=None):
    """首阴回踩 5 日线：近期有涨停基因 + 前一日阳线 + 最新阴线 + 最低价靠近 MA5 + 此前非连续阴线。

    必须校验涨停基因：否则任何普通回调股都符合「前阳后阴 + 贴 MA5」，冰点期会选到一堆无关票。
    """
    today = today or now_beijing().strftime("%Y-%m-%d")

    def check(raw):
        try:
            pure = normalize_code(raw)[0]
        except Exception:
            return None
        rows = fetch_kline_rows(pure, 10)
        if len(rows) < 6:
            return None
        hist = _latest_complete_bars(rows, today)
        if len(hist) < 6:
            return None
        prev, last = hist[-2], hist[-1]
        lim = _limit_pct_for(pure)
        # 涨停基因：近 5 日内至少一根收盘涨幅 >= 阈值-0.5%（容忍四舍五入）
        gene = False
        for i in range(max(1, len(hist) - 6), len(hist)):
            pc = _num(hist[i - 1][2])
            if pc > 0 and (_num(hist[i][2]) / pc - 1.0) >= (lim - 0.005):
                gene = True
                break
        if not gene:
            return None
        if _num(prev[2]) <= _num(prev[1]):
            return None
        if _num(last[2]) >= _num(last[1]):
            return None
        if _num(hist[-3][2]) < _num(hist[-3][1]):
            return None
        closes = [_num(r[2]) for r in hist]
        ma5 = sum(closes[-5:]) / min(5, len(closes))
        if ma5 <= 0:
            return None
        low, close = _num(last[4]), _num(last[2])
        if not (low <= ma5 * 1.03 and close >= ma5 * 0.97):
            return None
        return {
            "code": pure,
            "close": close,
            "ma5": round(ma5, 2),
            "amountYi": _row_amount_est_yi(last),
            "score": 82.0,
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = [r for r in ex.map(check, list(codes)[:40]) if r]
    results.sort(key=lambda x: abs(x["close"] / x["ma5"] - 1.0))
    return results[:limit]


def _norm_date(s):
    """统一日期格式为 YYYY-MM-DD（龙虎榜披露 '2026-11-25' vs 涨停池 qdate '20261125' 格式不一致，
    直接字符串比较会导致日期锁永远不解锁、龙虎榜席位永久空缺）。"""
    s = str(s or "").strip().replace("-", "").replace("/", "")[:8]
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:8]) if len(s) == 8 else str(s or "")


def _is_single_day_explanation(exp):
    """判断席位记录的上榜原因是否为「单日榜」。

    东财席位接口同日同时返回一日榜与三日榜（如九阳 11-25：单日净买 6332 万、三日累计 37195 万），
    混在一起会让 buy_total 翻倍、同一席位重复出现。单日榜的原因文案不含「连续三个交易日」。
    """
    e = str(exp or "")
    return "连续三个" not in e and "3个" not in e and "三日" not in e


def _fetch_lhb_rows(base, code, tdate, rpt, sort_col):
    """拉取单只股票单侧（买/卖）席位明细，取满前 5 席。

    2026-11-25 审计修复：按 EXPLANATION 区分一日榜/三日榜——优先只取单日榜；
    若该票当日无单日榜（仅三日榜上榜），才退回用三日榜。
    """
    p_seat = {
        "reportName": rpt,
        "columns": "OPERATEDEPT_NAME,BUY,SELL,NET,EXPLANATION",
        "filter": '(SECURITY_CODE="%s")(TRADE_DATE=\'%s\')' % (code, tdate),
        "pageNumber": "1",
        "pageSize": "10",
        "sortTypes": "-1",
        "sortColumns": sort_col,
        "source": "WEB",
        "client": "WEB",
    }
    try:
        s_raw = json.loads(http_get(base + "?" + urllib.parse.urlencode(p_seat), use_curl=True))
        rows = ((s_raw.get("result") or {}).get("data")) or []
    except Exception:
        return []
    single = [r for r in rows if _is_single_day_explanation(r.get("EXPLANATION"))]
    return (single or rows)[:5]


def fetch_billboard_map(extra_codes=None, qdate=None):
    """拉取最新一期东财龙虎榜及买卖前五席位画像。

    extra_codes: 额外需要穿透席位的代码（如跌停/炸板池），用于抓「拉萨接盘 + 主力净卖」的陷阱股——
                  这类票龙虎榜净买额往往靠后甚至为负，只看净买前排会漏杀。
    qdate:        选股交易日（YYYYMMDD）。龙虎榜最新披露日 != qdate 时（如 15:05 当日榜未出），
                  返回空 bmap，避免拿昨日龙虎榜给今日加减分。
    """
    base = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,CLOSE_PRICE,CHANGE_RATE,BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,ACCUM_AMOUNT,EXPLANATION",
        "pageNumber": "1",
        "pageSize": "80",
        "sortTypes": "-1,-1",
        "sortColumns": "TRADE_DATE,BILLBOARD_NET_AMT",
        "source": "WEB",
        "client": "WEB",
    }
    url = base + "?" + urllib.parse.urlencode(params)
    try:
        raw = json.loads(http_get(url, use_curl=True))
        rows = ((raw.get("result") or {}).get("data")) or []
    except Exception:
        rows = []
    if not rows:
        return "", {}, set()

    latest_date = str(rows[0].get("TRADE_DATE", ""))[:10]

    # 日期锁：龙虎榜最新披露日必须等于选股交易日，否则不使用（避免 15:05 用昨日榜给今日加减分）
    # 注意格式统一：latest_date 是 'YYYY-MM-DD'，qdate 是东财池的 'YYYYMMDD'，必须规范化后比较
    if qdate and latest_date and latest_date != _norm_date(qdate):
        return latest_date, {}, set()

    bmap = {}
    for r in rows:
        tdate = str(r.get("TRADE_DATE", ""))[:10]
        if tdate != latest_date:
            continue
        code = str(r.get("SECURITY_CODE") or "")
        if not code or "ST" in str(r.get("SECURITY_NAME_ABBR", "")):
            continue
        # 一日榜优先：同一票同日可能同时有单日榜与三日榜两条记录，三日累计净买往往是单日的数倍
        # （九阳 11-25：单日 6332 万 vs 三日 37195 万）。已有单日榜则不被三日榜覆盖；反之用单日榜替换。
        is_single = _is_single_day_explanation(r.get("EXPLANATION"))
        if code in bmap:
            if not (is_single and not bmap[code].get("_single")):
                continue
        net_wan = float(r.get("BILLBOARD_NET_AMT") or 0) / 1e4
        tot_wan = float(r.get("ACCUM_AMOUNT") or 1) / 1e4
        net_ratio = round(net_wan / max(1.0, tot_wan) * 100.0, 2)
        bmap[code] = {
            "code": code,
            "name": r.get("SECURITY_NAME_ABBR"),
            "date": tdate,
            "chg": float(r.get("CHANGE_RATE") or 0),
            "netWan": round(net_wan, 0),
            "netRatio": net_ratio,
            "amountWan": round(tot_wan, 0),
            "reason": r.get("EXPLANATION") or "",
            "_single": is_single,
            "seats": [],
        }

    # 穿透范围：净买前 30 + 净卖前 10（陷阱股） + 额外候选（跌停/炸板池）
    by_net = sorted(bmap.keys(), key=lambda c: -bmap[c]["netWan"])
    probe = set(by_net[:30]) | set(by_net[-10:])
    for c in (extra_codes or []):
        c = str(c or "").zfill(6)
        if c in bmap:
            probe.add(c)
    vetoed = set()
    for c in probe:
        tdate = bmap[c]["date"]
        seats_list = []
        buy_depts, sell_depts = [], []
        buy_total = sell_total = buy1_wan = 0.0
        seen_sig = set()
        for rpt, side_label, sort_col in [("RPT_BILLBOARD_DAILYDETAILSBUY", "买", "BUY"), ("RPT_BILLBOARD_DAILYDETAILSSELL", "卖", "SELL")]:
            idx_cnt = 0
            for sr in _fetch_lhb_rows(base, c, tdate, rpt, sort_col):
                dept = sr.get("OPERATEDEPT_NAME") or "未知席位"
                b_w = float(sr.get("BUY") or 0) / 1e4
                s_w = float(sr.get("SELL") or 0) / 1e4
                sig = (dept, round(b_w), round(s_w))
                if sig in seen_sig:
                    continue
                seen_sig.add(sig)
                idx_cnt += 1
                n_w = float(sr.get("NET") or (b_w * 1e4 - s_w * 1e4)) / 1e4
                tag = tag_seat(dept)
                if side_label == "买":
                    buy_total += b_w
                    if idx_cnt == 1:
                        buy1_wan = b_w
                    buy_depts.append(dept)
                else:
                    sell_total += s_w
                    sell_depts.append(dept)
                short_dept = dept.replace("证券股份有限公司", "").replace("有限责任公司", "").replace("证券营业部", "")
                seats_list.append({
                    "side": "%s%d" % (side_label, idx_cnt),
                    "tag": tag,
                    "dept": "%s (买%.0f万 / 卖%.0f万)" % (short_dept, b_w, s_w),
                    "net": "%+.0f万" % n_w
                })
        bmap[c]["seats"] = seats_list

        # 负面席位一：买榜 >=3 个拉萨散户席位，且卖榜净卖出 -> 一票否决
        lasha_cnt = sum(1 for d in buy_depts if "拉萨" in d)
        if lasha_cnt >= 3 and sell_total > buy_total:
            vetoed.add(c)
            bmap[c]["veto"] = True
            bmap[c]["vetoReason"] = "买榜%d个拉萨散户席位且卖榜净卖出" % lasha_cnt
            continue

        # 负面席位二：买榜 >=2 个高频量化席位且买卖双边同现 -> 做T扣30分并打标签
        quant_buy_cnt = sum(1 for s in seats_list if s["side"].startswith("买") and str(s["tag"]).startswith("量化"))
        if quant_buy_cnt >= 2 and (set(buy_depts) & set(sell_depts)):
            bmap[c]["quantT"] = True

        # 负面席位三：买一占总成交 >15% -> 独食板预警
        amount_wan = bmap[c].get("amountWan") or 0
        if buy1_wan > 0 and amount_wan > 0 and buy1_wan / amount_wan > 0.15:
            bmap[c]["duoshi"] = True

    for c in vetoed:
        bmap.pop(c, None)
    return latest_date, bmap, vetoed


def enrich_with_tencent_l1(items):
    """批量调用腾讯 qt.gtimg.cn 补齐实时五档盘口、量比、内外盘主动差、流通市值。"""
    if not items:
        return items
    norm_map = {}
    for it in items:
        pure, pcode, _, _ = normalize_code(it["code"])
        it["code"] = pure
        it["prefix"] = pcode
        norm_map[pcode] = it

    url = "https://qt.gtimg.cn/q=" + ",".join(norm_map.keys())
    text = http_get(url, encoding="gbk")
    for line in text.strip().splitlines():
        if "~" not in line:
            continue
        p = line.split("~")
        if len(p) < 50:
            continue
        pure = p[2]
        target = next((x for x in items if x["code"] == pure), None)
        if not target:
            continue
        now_px = float(p[3] or 0)
        pre_c = float(p[4] or 0)
        open_px = float(p[5] or 0)
        vol_hand = int(float(p[6] or 0))
        outer_vol = int(float(p[7] or 0))
        inner_vol = int(float(p[8] or 0))
        chg_pct = float(p[32] or 0)
        amt_wan = float(p[37] or 0)
        turnover = float(p[38] or 0)
        circ_mv = float(p[44] or 0)
        limit_up = float(p[47] or 0)
        limit_down = float(p[48] or 0)
        vol_ratio = float(p[49] or 0)
        bids = [[float(p[9 + i * 2] or 0), int(float(p[10 + i * 2] or 0))] for i in range(5)]
        asks = [[float(p[19 + i * 2] or 0), int(float(p[20 + i * 2] or 0))] for i in range(5)]

        is_zt = limit_up > 0 and abs(now_px - limit_up) <= 0.015
        amt_yi = round(amt_wan / 10000.0, 2)
        # 未封涨停时封单相关字段一律归零，不能回退取上一交易日的封单（否则非涨停票显示昨日封单，严重误导）
        if is_zt and bids[0][1] > 0:
            seal_yi = round((bids[0][0] * bids[0][1] * 100.0) / 1e8, 2)
            seal_ratio = round((seal_yi / max(0.01, amt_yi)) * 100.0, 1)
            seal_mv_ratio = round((seal_yi / max(1.0, circ_mv)) * 100.0, 2)
        else:
            seal_yi = 0.0
            seal_ratio = 0.0
            seal_mv_ratio = 0.0

        target.update({
            "name": p[1] or target.get("name", ""),
            "price": now_px,
            "preClose": pre_c,
            "open": open_px,
            "openGap": round((open_px / pre_c - 1.0) * 100.0, 2) if pre_c > 0 else 0.0,
            "chg": chg_pct,
            "volHand": vol_hand,
            "outerVol": outer_vol,
            "innerVol": inner_vol,
            "activeDiff": outer_vol - inner_vol,
            "deltaHands": 0,
            "amountYi": amt_yi,
            "turnover": turnover,
            "circMv": circ_mv,
            "volRatio": vol_ratio,
            "limitUp": limit_up,
            "limitDown": limit_down,
            "sealYi": seal_yi,
            "sealRatio": seal_ratio,
            "sealFlowRatio": seal_mv_ratio,
            "sealMvRatio": seal_mv_ratio,
            "bids": bids,
            "asks": asks,
            "quoteTime": p[30] or ""
        })
    return items


def generate_top10_picks(date_str=None, mode="post"):
    """按四维游资量化法则自动遴选 Top 10 核心股票池。"""
    # 统一用北京时间（UTC+8），不依赖运行环境 TZ；避免容器/本机时区差异造成调度错位
    now_bj = now_beijing()
    if not date_str:
        date_str = now_bj.strftime("%Y%m%d")

    zt_data = fetch_em_pool("getTopicZTPool", date_str)
    zb_data = fetch_em_pool("getTopicZBPool", date_str)
    dt_data = fetch_em_pool("getTopicDTPool", date_str)

    qdate = zt_data.get("qdate") or date_str
    zt_pool = [x for x in (zt_data.get("pool") or []) if "ST" not in str(x.get("n", ""))]
    zb_pool = [x for x in (zb_data.get("pool") or []) if "ST" not in str(x.get("n", ""))]
    dt_pool = [x for x in (dt_data.get("pool") or []) if "ST" not in str(x.get("n", ""))]

    # 龙虎榜：传入 qdate 做日期锁，传入跌停/炸板池代码扩大穿透范围（专抓拉萨接盘陷阱股）
    probe_codes = [str(x.get("c") or "") for x in (dt_pool + zb_pool) if x.get("c")]
    lhb_date, bmap, vetoed_lhb = fetch_billboard_map(extra_codes=probe_codes, qdate=qdate)
    if vetoed_lhb:
        zt_pool = [x for x in zt_pool if str(x.get("c")) not in vetoed_lhb]
        zb_pool = [x for x in zb_pool if str(x.get("c")) not in vetoed_lhb]

    # 北交所（92/8/4 开头）30% 涨跌幅 + 流动性薄，1 板卡位逻辑适用性差，主线卡位池排除
    def _is_bj(code):
        c = str(code or "").zfill(6)
        return c.startswith(("92", "8", "4"))

    zt_cnt, zb_cnt, dt_cnt = len(zt_pool), len(zb_pool), len(dt_pool)
    zb_rate = round(zb_cnt / max(1, zt_cnt + zb_cnt) * 100.0, 1)
    max_b = max([int(_num(x.get("lbc"), 1)) for x in zt_pool], default=1)

    # 盘前（如 09:25）当日三池可能尚未生成，此时不能用空池判定为「冰点」，
    # 否则会误触发首阴回踩的批量 K 线请求。缺失值用 -1 表示「不可用」。
    sentiment_snap, sentiment_src = load_sentiment_snapshot()
    sent_zt = _num(sentiment_snap.get("zt"), -1)
    sent_dt = _num(sentiment_snap.get("dt"), -1)
    data_ready = zt_cnt > 0
    # 2026-11-25 审计修复：实时三池可用时以实时数据为准，sentiment 快照（T-1 盘后）仅作盘前空池兜底。
    # 原先快照的 sent_dt>15 与实时的 dt_cnt=0（跌停池参数 bug）同时生效，导致 cold=True 但 dtCount=0 自相矛盾。
    if data_ready:
        cold = zt_cnt < 35 or dt_cnt > 15
        hot = zt_cnt >= 80 and zb_rate < 25 and dt_cnt <= 10
    else:
        cold = (sent_zt >= 0 and sent_zt < 35) or sent_dt > 15
        hot = sent_zt >= 80 and 0 <= sent_dt <= 10 and zb_rate < 25
    if hot:
        phase = "高潮/主升加速期"
    elif cold:
        phase = "冰点/退潮期"
    elif zb_rate > 35:
        phase = "高位强分歧期"
    else:
        phase = "主线发酵/良性轮动期"
    if not data_ready:
        phase = "%s（当日三池未就绪）" % phase

    # 情绪周期动态配额（合计 10 席）
    if cold:
        quotas = {"high": 0, "firstYin": 2, "mainline": 3, "lhb": 2, "fill": 1, "divergence": 1, "broken": 1}
    elif hot:
        quotas = {"high": 2, "firstYin": 0, "mainline": 5, "lhb": 2, "fill": 0, "divergence": 1, "broken": 0}
    else:
        quotas = {"high": 2, "firstYin": 0, "mainline": 3, "lhb": 3, "fill": 1, "divergence": 1, "broken": 0}

    hy_counts = {}
    for x in zt_pool:
        hy = x.get("hybk") or "其他"
        hy_counts[hy] = hy_counts.get(hy, 0) + 1
    top_sectors = [k for k, _ in sorted(hy_counts.items(), key=lambda kv: -kv[1])[:5]]

    quote_map = fetch_l1_map([x.get("c") for x in zt_pool])
    scored_zt = []
    for x in zt_pool:
        code = str(x.get("c") or "")
        name = str(x.get("n") or "")
        lbc = int(_num(x.get("lbc"), 1))
        zbc = int(_num(x.get("zbc"), 0))
        fbt = fmt_time(x.get("fbt"))
        fund_yi = round(_num(x.get("fund")) / 1e8, 2)
        amo_yi = round(max(0.05, _num(x.get("amount")) / 1e8), 2)
        seal_ratio = round((fund_yi / amo_yi) * 100.0, 1)
        hy = x.get("hybk") or "其他"
        q = quote_map.get(code, {})
        circ_mv = _num(q.get("circMv"))
        hs = _num(q.get("turnover"), _num(x.get("hs")))
        seal_flow_ratio = round((fund_yi / max(0.01, circ_mv)) * 100.0, 2) if circ_mv > 0 else 0.0
        # 分板制评分（2026-11-25 策略升级）：创业板/科创板 20cm 一个板的溢价约等于主板 2 板，
        # 原先 10cm/20cm/30cm 同一套 lbc*22 加分，对双创系统性低估。按板别折算「等效连板数」。
        limit_pct = _limit_pct_for(code)
        board_mult = 1.0 if limit_pct <= 0.10 else (1.8 if limit_pct <= 0.20 else 2.4)
        eff_boards = lbc * board_mult
        seal_bonus = min(40.0, seal_ratio * 0.8)
        flow_bonus = min(40.0, seal_flow_ratio * 8.0)
        if 8.0 <= hs <= 28.0:
            turn_bonus, turn_tag = 15.0, "黄金换手"
        elif hs < 3.0:
            turn_bonus, turn_tag = -20.0, "缩量一字"
        elif hs > 40.0:
            turn_bonus, turn_tag = -25.0, "死亡换手"
        else:
            turn_bonus, turn_tag = 0.0, ""
        sector_bonus = 15.0 if hy in top_sectors[:3] else (8.0 if hy in top_sectors else 0.0)
        # 2026-11-25 审计修复：东财 fbt 是 5 位整数 92500（=09:25:00），str() 后为 '92500'，
        # 与 '094000' 逐位字符串比较时 '9' > '0' 恒为 False，早盘加分从未触发过。必须 zfill(6) 补零。
        early_bonus = 12.0 if str(x.get("fbt") or "150000").zfill(6) <= "094000" else 0.0
        lhb_bonus = 20.0 if code in bmap and bmap[code]["netWan"] > 3000 else 0.0
        quant_penalty = -30.0 if (bmap.get(code) or {}).get("quantT") else 0.0
        score = (eff_boards * 22.0 + seal_bonus + flow_bonus + sector_bonus + early_bonus +
                 lhb_bonus + turn_bonus + quant_penalty - zbc * 4.5)
        # 烂板标记：炸板 >=3 次且封成比 <5% —— 不占 Top 10 进攻席位，隔离到 riskAnchors 观察区
        is_rotten_board = zbc >= 3 and seal_ratio < 5.0
        scored_zt.append({
            "raw": x, "code": code, "name": name, "sector": hy,
            "boards": lbc, "effBoards": round(eff_boards, 1), "brokenCount": zbc, "firstSeal": fbt,
            "sealYi": fund_yi, "amountYi": amo_yi, "sealRatio": seal_ratio,
            "sealFlowRatio": seal_flow_ratio, "circMv": circ_mv,
            "turnover": hs, "turnTag": turn_tag, "score": round(score, 1),
            "rottenBoard": is_rotten_board,
            "scoreParts": {
                "board": round(eff_boards * 22.0, 1), "sealRatio": round(seal_bonus, 1),
                "sealFlow": round(flow_bonus, 1), "sector": sector_bonus,
                "early": early_bonus, "lhb": lhb_bonus, "turnover": turn_bonus,
                "quantPenalty": quant_penalty, "brokenPenalty": -zbc * 4.5,
            }
        })

    if cold:
        scored_zt = [x for x in scored_zt if x["boards"] < 4]

    picked = []
    picked_codes = set()
    risk_anchors = []   # 烂板/高危隔离区：不占进攻席位，仅供风控观察

    def add_candidate(item, role_label, sig_type, sig_tag, rule_reason):
        c = item["code"]
        if c in picked_codes or len(picked) >= 10:
            return False
        # 烂板高危隔离（2026-11-25 策略升级）：炸板>=3 且封成比<5% 的票不进 Top 10，
        # 单独放进 riskAnchors。梯队 5 的「炸板池风控锚点」例外——它的定位就是风险观察。
        if item.get("rottenBoard") and sig_type != "BROKEN_WARN":
            if not any(a["code"] == c for a in risk_anchors):
                risk_anchors.append({
                    "code": c, "name": item.get("name") or c, "boards": item.get("boards", 0),
                    "effBoards": item.get("effBoards") or item.get("boards", 0),   # 分板制等效连板数（20cm×1.8 / 30cm×2.4）
                    "brokenCount": item.get("brokenCount", 0), "sealRatio": item.get("sealRatio", 0.0),
                    "riskTag": "烂板高危(炸板%d次/封成比%.1f%%)" % (item.get("brokenCount", 0), item.get("sealRatio", 0.0)),
                })
            return False
        picked_codes.add(c)
        lhb_info = bmap.get(c) or {}
        extra_tags = []
        if lhb_info.get("duoshi"):
            extra_tags.append("独食板预警")
        if lhb_info.get("quantT"):
            extra_tags.append("量化做T预警")
        if item.get("turnTag"):
            extra_tags.append(item["turnTag"])
        sig_tag_full = sig_tag + (" · " + "/".join(extra_tags) if extra_tags else "")
        if lhb_info.get("seats"):
            lhb_summary = "龙虎榜净买 %+.2f亿 (占成交 %+.1f%%)" % (lhb_info["netWan"] / 10000.0, lhb_info["netRatio"])
            lhb_seats = lhb_info["seats"]
        else:
            lhb_summary = "%s · 量化评分 %.1f 分" % (rule_reason, item.get("score", 85.0))
            lhb_seats = [
                {"side": "选股", "tag": role_label, "dept": rule_reason, "net": "封单%.2f亿" % item.get("sealYi", 0)},
                {"side": "共振", "tag": item["sector"], "dept": "所属板块当日涨停 %d 家 · 首封 %s" % (hy_counts.get(item["sector"], 1), item.get("firstSeal", "--")), "net": "封成比%.1f%%" % item.get("sealRatio", 0)}
            ]
        picked.append({
            "code": c, "prefix": "", "name": item["name"], "sector": item["sector"],
            "boards": item["boards"], "boardLabel": role_label,
            "effBoards": item.get("effBoards") or item["boards"],   # 分板制等效连板数（20cm×1.8 / 30cm×2.4）
            "signalType": sig_type, "signalTag": sig_tag_full,
            "price": 0, "preClose": 0, "open": 0, "openGap": 0, "chg": 0,
            "volHand": 0, "outerVol": 0, "innerVol": 0, "activeDiff": 0, "deltaHands": 0,
            "firstSeal": item.get("firstSeal", "--:--:--"),
            "brokenCount": item.get("brokenCount", 0),
            "sealYi": item.get("sealYi", 0.0), "amountYi": item.get("amountYi", 0.0),
            "sealRatio": item.get("sealRatio", 0.0),
            "sealFlowRatio": item.get("sealFlowRatio", 0.0),
            "sealMvRatio": item.get("sealFlowRatio", 0.0),
            "circMv": item.get("circMv", 0.0), "turnover": item.get("turnover", 0.0),
            "volRatio": 1.0, "limitUp": 0, "limitDown": 0, "quoteTime": "",
            "bids": [[0, 0]] * 5, "asks": [[0, 0]] * 5,
            "lhbSummary": lhb_summary, "lhbSeats": lhb_seats,
            "scoreParts": item.get("scoreParts", {}),
            "backtest": {
                "wtsRet": "量化评分 %.1f" % item.get("score", 85.0),
                "wtsMdd": "首封 %s" % item.get("firstSeal", "--"),
                "wtsSharpe": "%d连板" % item["boards"],
                "wtsWin": "板块涨停 %d 家" % hy_counts.get(item["sector"], 1),
                "mrRet": "封单 %.2f亿" % item.get("sealYi", 0.0),
                "boRet": "封成比 %.1f%% / 封流比 %.2f%%" % (item.get("sealRatio", 0.0), item.get("sealFlowRatio", 0.0)),
                "note": rule_reason
            }
        })
        return True

    # 梯队1：空间高标（冰点/退潮期配额归零，且 >=4 板全部剔除）
    high_boards = sorted(scored_zt, key=lambda x: (-x["boards"], -x["sealFlowRatio"], x["firstSeal"]))
    high_added = 0
    for hb in high_boards:
        if high_added >= quotas["high"]:
            break
        if add_candidate(
            hb,
            "%d连板高标" % hb["boards"],
            "WEAK_TO_STRONG" if hb["brokenCount"] <= 2 else "BROKEN_WARN",
            "首封%s·封成比%.1f%%·封流比%.2f%%·炸%d次" % (hb["firstSeal"], hb["sealRatio"], hb["sealFlowRatio"], hb["brokenCount"]),
            "维度1[最高空间板]：%d连板市场高度标杆，次日重点观察竞价弱转强或换手晋级" % hb["boards"]
        ):
            high_added += 1

    # 梯队1B：冰点/退潮期替代——首阴回踩 5 日线 + 1进2 首板
    if quotas["firstYin"] > 0:
        # 候选必须是「昨日涨停、今日首阴」的票。2026-11-25 审计修复：
        # 原兜底用 scored_zt（今日涨停池）——今天涨停的票不可能是首阴，日期锚定修复后会被
        # 「最新 K 线为阴线」自动排除，但为避免无谓请求，这里直接排除今日涨停代码。
        today_zt_codes = {x["code"] for x in scored_zt}
        universe = [str(c).zfill(6) for c in (sentiment_snap.get("ztCodes") or [])[:40]]
        universe = [c for c in universe if c not in today_zt_codes]
        if not universe:
            # 无 T-1 快照时，用炸板池（昨日曾涨停今日走弱）作次级候选，而非今日涨停池
            universe = [str(x.get("c") or "").zfill(6) for x in zb_pool[:30]]
            universe = [c for c in universe if c not in today_zt_codes]
        yin_added = 0
        for fy in fetch_first_yin_ma5_candidates(universe, limit=quotas["firstYin"]):
            if yin_added >= quotas["firstYin"]:
                break
            if add_candidate(
                {"code": fy["code"], "name": fy["code"], "sector": "首阴回踩", "boards": 1,
                 "brokenCount": 0, "firstSeal": "MA5回踩", "sealYi": 0.0,
                 "amountYi": fy.get("amountYi", 0.0), "sealRatio": 0.0,
                 "sealFlowRatio": 0.0, "circMv": 0.0, "turnover": 0.0,
                 "score": fy["score"], "turnTag": "首阴回踩", "scoreParts": {}},
                "首阴回踩5日线", "FIRST_YIN_MA5",
                "收盘%.2f/MA5 %.2f" % (fy["close"], fy["ma5"]),
                "维度1[冰点防守]：近期涨停股首阴回踩 5 日线，规避高位连板补跌"
            ):
                yin_added += 1

    # 梯队2：主线 1进2 / 2进3 高封成比卡位先锋（主升期扩至 5 席）
    # 2026-11-25 审计修复：换手下限 2.5%→5.0%。特一药业换手 2.78% 被打「缩量一字」扣 20 分，
    # 却仍按封流比排进第 4 席——缩量一字次日无换手基础，极易炸板。被标「缩量一字」的直接排除在卡位池外。
    mainline_kawi = sorted(
        [x for x in scored_zt if 1 <= x["boards"] <= 3 and 5.0 <= x["turnover"] <= 38.0 and x["brokenCount"] <= 1
         and x.get("turnTag") != "缩量一字" and not _is_bj(x["code"])],
        key=lambda x: (-(1 if x["sector"] in top_sectors[:3] else 0), -x["boards"], -x["sealFlowRatio"])
    )
    main_added = 0
    for mk in mainline_kawi:
        if main_added >= quotas["mainline"]:
            break
        if add_candidate(
            mk,
            "%d板·主线卡位" % mk["boards"],
            "HIGH_SEAL" if mk["sealRatio"] >= 20 or mk["sealFlowRatio"] >= 2 else "WEAK_TO_STRONG",
            "%s主线·首封%s·封成比%.1f%%·封流比%.2f%%" % (mk["sector"], mk["firstSeal"], mk["sealRatio"], mk["sealFlowRatio"]),
            "维度2[主线高封成比卡位]：%s板块(%d家涨停)前排身位，封单%.2f亿/封成比%.1f%%/封流比%.2f%%" % (
                mk["sector"], hy_counts.get(mk["sector"], 1), mk["sealYi"], mk["sealRatio"], mk["sealFlowRatio"]
            )
        ):
            main_added += 1

    # 梯队3：龙虎榜顶级游资/北向/机构大额净买合力股
    zt_by_code = {x["code"]: x for x in scored_zt}
    top_lhb_codes = sorted(bmap.keys(), key=lambda c: -bmap[c]["netWan"])
    lhb_added = 0
    for lc in top_lhb_codes:
        if lhb_added >= quotas["lhb"]:
            break
        linfo = bmap[lc]
        if linfo["netWan"] <= 1500:
            continue
        # 2026-11-25 审计修复：龙虎榜合力必须是「当日涨停」或「强势收盘」——
        # 原先不在涨停池的票用硬编码默认值(boards=0/turnover=15/score=90)直接入池，
        # 华胜天成(+4.37%、换手31%冲高回落)、光洋股份(跌1.26%)这类非涨停跟风股混进超短核心池。
        # 现要求：当日涨停，或收盘涨幅 >= 7% 且上影线 <= 3%（排除冲高回落出货）。
        chg = float(linfo.get("chg") or 0)
        in_zt = lc in zt_by_code
        if not in_zt:
            if chg < 7.0:
                continue
            # 上影线检查：用当日 K 线（high - close）/ prev_close
            try:
                krows = fetch_kline_rows(lc, 3)
                if krows:
                    k = krows[-1]
                    pc = _num(krows[-2][2]) if len(krows) >= 2 else _num(k[1])
                    upper_shadow = (_num(k[3]) - _num(k[2])) / pc * 100.0 if pc > 0 else 99
                    if upper_shadow > 3.0:
                        continue
            except Exception:
                continue
        base_item = zt_by_code.get(lc) or {
            "code": lc, "name": linfo["name"], "sector": "龙虎榜主力",
            "boards": 1 if chg >= 9.5 else 0, "brokenCount": 0,
            "firstSeal": "龙虎榜净买第1梯队", "sealYi": round(linfo["netWan"] / 10000.0, 2),
            "amountYi": 10.0, "sealRatio": linfo["netRatio"], "sealFlowRatio": 0.0,
            "circMv": 0.0, "turnover": 15.0, "score": 90.0, "scoreParts": {},
            "chg": chg,
        }
        seat_tags = " + ".join(list(dict.fromkeys([s["tag"] for s in linfo.get("seats", []) if s["side"].startswith("买")]))) or "主力游资"
        if add_candidate(
            base_item,
            "%d板·龙虎榜合力" % max(1, base_item["boards"]),
            "HOT_MONEY",
            "%s·净买%+.2f亿" % (seat_tags[:16], linfo["netWan"] / 10000.0),
            "维度3[龙虎榜游资/机构合力]：龙虎榜净买入 %+.0f万 (占总成交 %+.1f%%)，席位：%s" % (
                linfo["netWan"], linfo["netRatio"], seat_tags
            )
        ):
            lhb_added += 1

    # 梯队4：按综合评分补足基础席位
    fill_added = 0
    for x in sorted(scored_zt, key=lambda k: -k["score"]):
        if fill_added >= quotas["fill"]:
            break
        if add_candidate(
            x, "%d连板精选" % x["boards"], "WEAK_TO_STRONG",
            "首封%s·封单%.2f亿·封流比%.2f%%" % (x["firstSeal"], x["sealYi"], x["sealFlowRatio"]),
            "维度2[综合量化高分]：连板梯队、封成比、封流比与换手率综合评分前列"
        ):
            fill_added += 1

    # 梯队5：高辨识度分歧烂板 / 炸板池风控与反包观察锚点
    high_div_zt = sorted([x for x in scored_zt if x["brokenCount"] >= 2], key=lambda k: (-k["boards"], -k["brokenCount"]))
    div_added = 0
    for hd in high_div_zt:
        if div_added >= quotas["divergence"]:
            break
        if add_candidate(
            hd, "%d板·强分歧回封" % hd["boards"], "BROKEN_WARN",
            "盘中炸板%d次·换手%.1f%%" % (hd["brokenCount"], hd["turnover"]),
            "维度4[高分歧风控/弱转强锚点]：盘中经历 %d 次炸板大分歧回封，次日高开为超预期弱转强，低开则防核按钮" % hd["brokenCount"]
        ):
            div_added += 1

    sorted_zb = sorted(zb_pool, key=lambda x: -_num(x.get("amount")))
    broken_added = 0
    for zb in sorted_zb:
        if broken_added >= quotas["broken"] or len(picked) >= 10:
            break
        zcode = str(zb.get("c") or "")
        zamo = round(_num(zb.get("amount")) / 1e8, 2)
        add_candidate(
            {"code": zcode, "name": str(zb.get("n") or ""), "sector": zb.get("hybk") or "炸板预警",
             "boards": 0, "brokenCount": int(_num(zb.get("zbc"), 1)), "firstSeal": fmt_time(zb.get("fbt")),
             "sealYi": 0.0, "amountYi": zamo, "sealRatio": 0.0, "sealFlowRatio": 0.0,
             "circMv": 0.0, "turnover": _num(zb.get("hs")), "score": 60.0, "scoreParts": {}},
            "炸板池·风控/反包", "BROKEN_WARN",
            "首触%s·炸板%d次·成交%.1f亿" % (fmt_time(zb.get("fbt")), int(_num(zb.get("zbc"), 1)), zamo),
            "维度4[炸板风控/次日反包观察]：当日 %s 曾封板后炸板回落，作为次日情绪修复反包或亏钱效应风控锚点" % str(zb.get("n") or "")
        )
        broken_added += 1

    picked = enrich_with_tencent_l1(picked)

    # 9:25 集合竞价：真弱转强必须同时满足高开 1.0%~7.5% 与竞价量占昨量 5%~18%
    # 时间守卫：只有北京时间 09:15~09:40 之间腾讯 p[37] 才是竞价成交额；
    # 其余时段 p[37] 为当日/昨日全天成交额，量能比会失真，故只写字段、不标记、不重排。
    auction_note = ""
    bj_hm = int(now_bj.strftime("%H%M"))
    in_auction_window = 915 <= bj_hm <= 940
    if mode == "auction":
        for p in picked:
            auction_amt = _num(p.get("amountYi"))
            prev_amt = fetch_prev_day_amount_est_yi(p["code"])
            ratio = round(auction_amt / max(0.01, prev_amt) * 100.0, 2) if prev_amt > 0 else 0.0
            p["auctionVolRatio"] = ratio
            p["prevDayAmountEstYi"] = prev_amt
            p["trueWeak"] = False
            if not in_auction_window:
                continue
            gap = _num(p.get("openGap"))
            # 「主力真抢筹」三重过滤（2026-11-25 策略升级）：
            # ① 竞价五档委比 > -20%：防止 9:25 匹配价虽高开、但卖一~卖五压着巨量抛单无承接的「竞价诱多出货」
            bids = p.get("bids") or []
            asks = p.get("asks") or []
            bid_vol = sum(b[1] for b in bids if len(b) >= 2)
            ask_vol = sum(a[1] for a in asks if len(a) >= 2)
            bid_ask_ratio = round((bid_vol - ask_vol) / max(1, bid_vol + ask_vol) * 100.0, 1)
            p["bidAskRatio"] = bid_ask_ratio
            # ② 排除前一日放量天量烂板后缩量高开（ratio < 6%）的假弱转强：
            #    brokenCount>=2 说明昨日反复炸板，若竞价量能再不足就是没人接力
            prev_rots = _num(p.get("brokenCount"))
            fake_reversal = prev_rots >= 2 and ratio < 6.0
            if 1.0 <= gap <= 7.5 and 5.0 <= ratio <= 18.0 and bid_ask_ratio > -20.0 and not fake_reversal:
                p["trueWeak"] = True
                p["signalType"] = "WEAK_TO_STRONG"
                p["signalTag"] = "【9:25真弱转强】高开%+.2f%%·竞价量占昨量%.1f%%·委比%+.1f%%" % (gap, ratio, bid_ask_ratio)
            elif gap <= -3.0:
                p["signalType"] = "BROKEN_WARN"
                p["signalTag"] = "【9:25竞价不及预期】低开%.2f%%·防核按钮" % gap
            elif 1.0 <= gap <= 7.5 and 5.0 <= ratio <= 18.0:
                # 量能达标但被委比/假弱转强过滤——保留字段但不置顶，标注原因
                why = "五档委比%+.1f%%≤-20%%(卖压过重)" % bid_ask_ratio if bid_ask_ratio <= -20.0 else "昨炸板%d次且竞价量比%.1f%%<6%%(假弱转强)" % (prev_rots, ratio)
                p["signalTag"] = "【竞价未过三重过滤】%s" % why
        if in_auction_window:
            picked.sort(key=lambda p: (0 if p.get("trueWeak") else 1,
                                       -_num(p.get("auctionVolRatio")),
                                       -_num(p.get("openGap"))))
            auction_note = "北京时间 %s 处于竞价窗口，已按真弱转强规则置顶重排。" % now_bj.strftime("%H:%M")
        else:
            auction_note = ("北京时间 %s 不在 09:15~09:40 竞价窗口：腾讯成交额非竞价口径，"
                            "已保留量能比字段但未标记真弱转强、未重排。" % now_bj.strftime("%H:%M"))

    return {
        "ok": True,
        "generatedAt": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "generatedAtTz": "Asia/Shanghai (UTC+8)",
        "auctionNote": auction_note,
        "tradeDate": qdate,
        "lhbDate": lhb_date,
        # 龙虎榜日期锁：lhbDate != tradeDate 表示当日榜未披露，本次未使用龙虎榜加减分
        "lhbLocked": bool(lhb_date) and lhb_date == _norm_date(qdate),
        # 烂板高危隔离区：不占 Top 10 进攻席位，仅供风控观察（2026-11-25 策略升级）
        "riskAnchors": risk_anchors,
        "mode": mode,
        "cycle": {
            "phase": phase, "cold": cold, "hot": hot, "dataReady": data_ready,
            "ztCount": zt_cnt, "dtCount": dt_cnt, "zbCount": zb_cnt,
            "sentimentDate": sentiment_snap.get("date") or "",
            "sentimentSource": sentiment_src,
            "quotas": quotas,
            "vetoedByLhb": sorted(vetoed_lhb),
        },
        "marketSummary": {
            "phase": phase,
            "ztCount": zt_cnt, "zbCount": zb_cnt, "dtCount": dt_cnt,
            "zbRate": zb_rate, "maxBoard": max_b,
            "topSectors": [{"name": k, "count": hy_counts[k]} for k in top_sectors]
        },
        "selectionRules": [
            "情绪周期：涨停<35或跌停>15时高标配额归零，改选首阴回踩5日线与1进2；主升期主线卡位扩至5席",
            "评分：封成比+封流比(封单/流通市值)*8，黄金换手8%~28%加15分，缩量一字<3%与死亡换手>40%扣分",
            "龙虎榜：买榜>=3拉萨散户且卖榜净卖出否决；量化做T扣30分；买一占比>15%独食板预警",
            "9:25真弱转强：高开1.0%~7.5%且竞价量占昨量5%~18%（昨量为量价代理）才置顶"
        ],
        "picks": picked
    }


def main():
    ap = argparse.ArgumentParser(description="EdsonStock A股超短游资 Top 10 自动选股引擎")
    ap.add_argument("--date", default=None, help="交易日 YYYYMMDD（默认最新交易日）")
    ap.add_argument("--mode", default="post", choices=["post", "auction"],
                    help="post: 15:05/17:30盘后选股 | auction: 09:25集合竞价弱转强动态重排")
    ap.add_argument("--out", default=str(BASE_DIR / "web" / "daily_picks.json"),
                    help="输出 JSON 文件路径（供前端网页自动读取）")
    args = ap.parse_args()

    res = generate_top10_picks(args.date, args.mode)
    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    ms, cy = res["marketSummary"], res["cycle"]
    print("=== EdsonStock 超短 Top 10 自动选股完成 (交易日: %s | 模式: %s) ===" % (res["tradeDate"], res["mode"]))
    print("  情绪周期: 【%s】 | 涨停 %d / 跌停 %d / 炸板 %d (炸板率 %.1f%%) | 最高板 %d连板" %
          (ms["phase"], ms["ztCount"], ms["dtCount"], ms["zbCount"], ms["zbRate"], ms["maxBoard"]))
    print("  动态配额: %s | 龙虎榜否决: %s | 龙虎榜日期锁: %s" % (
        cy["quotas"], cy["vetoedByLhb"] or "无",
        ("已解锁(%s)" % res["lhbDate"]) if res.get("lhbLocked") else "锁定(当日榜未披露，未用龙虎榜加减分)"))
    print("  主线板块: " + " | ".join(["%s(%d家)" % (s["name"], s["count"]) for s in ms["topSectors"]]))
    print("  已写入文件: %s (共 %d 只核心标的)" % (out_path, len(res["picks"])))
    for idx, p in enumerate(res["picks"], 1):
        sp = p.get("scoreParts") or {}
        score = sum(_num(sp.get(k)) for k in ("board", "sealRatio", "sealFlow", "sector", "early", "lhb", "turnover", "quantPenalty", "brokenPenalty"))
        print("  %2d. [%s] %s (%s | %s) 评分%.1f 封成比%.1f%% 封流比%.2f%% | %s" %
              (idx, p["boardLabel"], p["name"], p["code"], p["sector"],
               score, p["sealRatio"], p["sealFlowRatio"], p["signalTag"]))


if __name__ == "__main__":
    main()
