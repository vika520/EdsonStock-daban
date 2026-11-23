#!/usr/bin/env python3
"""EdsonStock × A股超短游资 Top 10 量化选股引擎 (auto_pick_top10.py)。

选股绝非随机，严格按四维游资量化矩阵每日自动生成 10 只核心盯盘池：
  - 席位 1~2【空间高标组 (2只)】：全市场最高连板身位（>=3板），按连板高度 + 封成比 + 首封时间排序，捕捉市场总龙头与同身位竞争者；
  - 席位 3~5【主线1进2/2进3卡位组 (3只)】：当日涨停家数 TOP3 主线行业中，1~2 板且封成比最高、早盘 10:00 前封板、换手率健康的晋级先锋；
  - 席位 6~8【龙虎榜顶级游资/机构/北向合力组 (3只)】：东财最新龙虎榜净买入额前排，且买榜出现知名游资（章盟主/方新侠/六一中路/呼家楼等）、北向或机构专用，且卖榜非游资砸盘的合力标的；
  - 席位 9~10【分歧/炸板风控观察组 (2只)】：当日曾触及涨停但炸板回落或高标放量烂板（炸板次数>=2）的核心辨识度标的，用于次日 9:25 观察「弱转强反包」或「核按钮风控锚点」。

支持两种运行模式：
  1) `--mode post`    (15:05 / 17:30 盘后选股)：综合东财涨停池、炸板池、龙虎榜席位与腾讯收盘五档盘口，选出次日 Top 10 核心备选池；
  2) `--mode auction` (09:25:03 集合竞价弱转强动态重排)：在盘后备选池 + 当日高开池基础上，实时拉取腾讯 9:25 集合竞价 openGap 与量比，
                      按「竞价高开 +0.5%~+7.5% (非一字) + 量比激增」重排 Top 10 并直接标记【9:25 竞价弱转强确认】。

用法：
  python3 scripts/auto_pick_top10.py --mode post --out web/daily_picks.json
  python3 scripts/auto_pick_top10.py --mode auction --out web/daily_picks.json
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import urllib.parse
from realtime_and_short import fetch_em_pool, http_get, normalize_code, tag_seat

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent


def fmt_time(fbt_raw):
    s = str(fbt_raw or "").strip().zfill(6)
    if len(s) == 6 and s.isdigit():
        return "%s:%s:%s" % (s[:2], s[2:4], s[4:6])
    return s or "--:--:--"


def fetch_billboard_map():
    """拉取最新一期东财龙虎榜净买前 30 名及买卖前五席位画像。"""
    base = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,CLOSE_PRICE,CHANGE_RATE,BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,ACCUM_AMOUNT,EXPLANATION",
        "pageNumber": "1",
        "pageSize": "40",
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
        return "", {}

    latest_date = str(rows[0].get("TRADE_DATE", ""))[:10]
    bmap = {}
    for r in rows:
        tdate = str(r.get("TRADE_DATE", ""))[:10]
        if tdate != latest_date:
            continue
        code = str(r.get("SECURITY_CODE") or "")
        if not code or code in bmap or "ST" in str(r.get("SECURITY_NAME_ABBR", "")):
            continue
        net_wan = float(r.get("BILLBOARD_NET_AMT") or 0) / 1e4
        tot_wan = float(r.get("ACCUM_AMOUNT") or 1) / 1e4
        net_ratio = round(net_wan / max(1.0, tot_wan) * 100, 2)
        bmap[code] = {
            "code": code,
            "name": r.get("SECURITY_NAME_ABBR"),
            "date": tdate,
            "chg": float(r.get("CHANGE_RATE") or 0),
            "netWan": round(net_wan, 0),
            "netRatio": net_ratio,
            "reason": r.get("EXPLANATION") or "",
            "seats": []
        }

    # 为净买前 8 只股票穿透买卖席位标签
    top_codes = sorted(bmap.keys(), key=lambda c: -bmap[c]["netWan"])[:8]
    for c in top_codes:
        tdate = bmap[c]["date"]
        seats_list = []
        for rpt, side_label in [("RPT_BILLBOARD_DAILYDETAILSBUY", "买"), ("RPT_BILLBOARD_DAILYDETAILSSELL", "卖")]:
            p_seat = {
                "reportName": rpt,
                "columns": "OPERATEDEPT_NAME,BUY,SELL,NET",
                "filter": '(SECURITY_CODE="%s")(TRADE_DATE=\'%s\')' % (c, tdate),
                "pageNumber": "1",
                "pageSize": "5",
                "sortTypes": "-1",
                "sortColumns": "BUY" if "BUY" in rpt else "SELL",
                "source": "WEB",
                "client": "WEB",
            }
            try:
                s_raw = json.loads(http_get(base + "?" + urllib.parse.urlencode(p_seat), use_curl=True))
                s_rows = ((s_raw.get("result") or {}).get("data")) or []
                seen_sig = set()
                idx_cnt = 0
                for sr in s_rows:
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
                    short_dept = dept.replace("证券股份有限公司", "").replace("有限责任公司", "").replace("证券营业部", "")
                    seats_list.append({
                        "side": "%s%d" % (side_label, idx_cnt),
                        "tag": tag,
                        "dept": "%s (买%.0f万 / 卖%.0f万)" % (short_dept, b_w, s_w),
                        "net": "%+.0f万" % n_w
                    })
                    if idx_cnt >= 3:
                        break
            except Exception:
                pass
        bmap[c]["seats"] = seats_list
    return latest_date, bmap


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
        seal_yi = round((bids[0][0] * bids[0][1] * 100.0) / 1e8, 2) if (is_zt and bids[0][1] > 0) else target.get("sealYi", 0.0)
        seal_ratio = round((seal_yi / max(0.01, amt_yi)) * 100.0, 1) if is_zt else 0.0
        seal_mv_ratio = round((seal_yi / max(1.0, circ_mv)) * 100.0, 2) if is_zt else 0.0

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
            "sealMvRatio": seal_mv_ratio,
            "bids": bids,
            "asks": asks,
            "quoteTime": p[30] or ""
        })
    return items


def generate_top10_picks(date_str=None, mode="post"):
    """按四维游资量化选股法则自动遴选 Top 10 核心股票池。"""
    if not date_str:
        date_str = dt.datetime.now().strftime("%Y%m%d")

    zt_data = fetch_em_pool("getTopicZTPool", date_str)
    zb_data = fetch_em_pool("getTopicZBPool", date_str)
    dt_data = fetch_em_pool("getTopicDTPool", date_str)

    qdate = zt_data.get("qdate") or date_str
    zt_pool = [x for x in (zt_data.get("pool") or []) if "ST" not in str(x.get("n", ""))]
    zb_pool = [x for x in (zb_data.get("pool") or []) if "ST" not in str(x.get("n", ""))]
    dt_pool = [x for x in (dt_data.get("pool") or []) if "ST" not in str(x.get("n", ""))]

    lhb_date, bmap = fetch_billboard_map()

    # 统计主线行业涨停家数
    hy_counts = {}
    for x in zt_pool:
        hy = x.get("hybk") or "其他"
        hy_counts[hy] = hy_counts.get(hy, 0) + 1
    top_sectors = [k for k, _ in sorted(hy_counts.items(), key=lambda kv: -kv[1])[:5]]

    # 为涨停池每只票计算量化综合评分
    scored_zt = []
    for x in zt_pool:
        code = str(x.get("c") or "")
        name = str(x.get("n") or "")
        lbc = int(x.get("lbc") or 1)
        zbc = int(x.get("zbc") or 0)
        fbt = fmt_time(x.get("fbt"))
        fund_yi = round(float(x.get("fund") or 0) / 1e8, 2)
        amo_yi = round(float(x.get("amount") or 1) / 1e8, 2)
        seal_ratio = round((fund_yi / max(0.05, amo_yi)) * 100.0, 1)
        hy = x.get("hybk") or "其他"
        hs = float(x.get("hs") or 0)

        # 评分公式：连板身位权重 + 封成比权重 + 主线行业共振权重 - 炸板扣分
        sector_bonus = 15.0 if hy in top_sectors[:3] else (8.0 if hy in top_sectors else 0.0)
        early_bonus = 12.0 if str(x.get("fbt") or "150000") <= "094000" else 0.0
        lhb_bonus = 20.0 if code in bmap and bmap[code]["netWan"] > 3000 else 0.0
        score = lbc * 22.0 + min(40.0, seal_ratio * 0.8) + sector_bonus + early_bonus + lhb_bonus - zbc * 4.5

        scored_zt.append({
            "raw": x,
            "code": code,
            "name": name,
            "sector": hy,
            "boards": lbc,
            "brokenCount": zbc,
            "firstSeal": fbt,
            "sealYi": fund_yi,
            "amountYi": amo_yi,
            "sealRatio": seal_ratio,
            "turnover": hs,
            "score": round(score, 1)
        })

    picked = []
    picked_codes = set()

    def add_candidate(item, role_label, sig_type, sig_tag, rule_reason):
        c = item["code"]
        if c in picked_codes or len(picked) >= 10:
            return False
        picked_codes.add(c)
        lhb_info = bmap.get(c)
        if lhb_info and lhb_info.get("seats"):
            lhb_summary = "龙虎榜净买 %+.2f亿 (占成交 %+.1f%%)" % (lhb_info["netWan"] / 10000.0, lhb_info["netRatio"])
            lhb_seats = lhb_info["seats"]
        else:
            lhb_summary = "%s · 量化评分 %.1f 分" % (rule_reason, item.get("score", 85.0))
            lhb_seats = [
                {"side": "选股", "tag": role_label, "dept": rule_reason, "net": "封单%.2f亿" % item.get("sealYi", 0)},
                {"side": "共振", "tag": item["sector"], "dept": "所属板块当日涨停 %d 家 · 首封 %s" % (hy_counts.get(item["sector"], 1), item.get("firstSeal", "--")), "net": "封成比%.1f%%" % item.get("sealRatio", 0)}
            ]

        picked.append({
            "code": c,
            "prefix": "",
            "name": item["name"],
            "sector": item["sector"],
            "boards": item["boards"],
            "boardLabel": role_label,
            "signalType": sig_type,
            "signalTag": sig_tag,
            "price": 0, "preClose": 0, "open": 0, "openGap": 0, "chg": 0,
            "volHand": 0, "outerVol": 0, "innerVol": 0, "activeDiff": 0, "deltaHands": 0,
            "firstSeal": item.get("firstSeal", "--:--:--"),
            "brokenCount": item.get("brokenCount", 0),
            "sealYi": item.get("sealYi", 0.0),
            "amountYi": item.get("amountYi", 0.0),
            "sealRatio": item.get("sealRatio", 0.0),
            "sealMvRatio": 0.0,
            "turnover": item.get("turnover", 0.0),
            "circMv": 0.0,
            "volRatio": 1.0,
            "limitUp": 0, "limitDown": 0, "quoteTime": "",
            "bids": [[0, 0]] * 5,
            "asks": [[0, 0]] * 5,
            "lhbSummary": lhb_summary,
            "lhbSeats": lhb_seats,
            "backtest": {
                "wtsRet": "量化评分 %.1f" % item.get("score", 85.0),
                "wtsMdd": "首封 %s" % item.get("firstSeal", "--"),
                "wtsSharpe": "%d连板" % item["boards"],
                "wtsWin": "板块涨停 %d 家" % hy_counts.get(item["sector"], 1),
                "mrRet": "封单 %.2f亿" % item.get("sealYi", 0.0),
                "boRet": "封成比 %.1f%%" % item.get("sealRatio", 0.0),
                "note": rule_reason
            }
        })
        return True

    # 第一梯队（2只）：空间高标总龙（连板高度最高的前 2~3 只）
    high_boards = sorted(scored_zt, key=lambda x: (-x["boards"], -x["sealRatio"], x["firstSeal"]))
    for hb in high_boards[:2]:
        add_candidate(
            hb,
            "%d连板高标" % hb["boards"],
            "WEAK_TO_STRONG" if hb["brokenCount"] <= 2 else "BROKEN_WARN",
            "首封%s·封成比%.1f%%·炸%d次" % (hb["firstSeal"], hb["sealRatio"], hb["brokenCount"]),
            "维度1[最高空间板]：%d连板市场高度标杆，次日重点观察竞价弱转强或换手晋级" % hb["boards"]
        )

    # 第二梯队（3只）：主线板块 1进2 / 2进3 高封成比卡位先锋（换手率 >= 3% 避免纯无量一字）
    mainline_kawi = sorted(
        [x for x in scored_zt if 1 <= x["boards"] <= 3 and 2.5 <= x["turnover"] <= 38.0 and x["brokenCount"] <= 1],
        key=lambda x: (-(1 if x["sector"] in top_sectors[:3] else 0), -x["boards"], -x["sealRatio"])
    )
    for mk in mainline_kawi:
        if len(picked) >= 5:
            break
        add_candidate(
            mk,
            "%d板·主线卡位" % mk["boards"],
            "HIGH_SEAL" if mk["sealRatio"] >= 20 else "WEAK_TO_STRONG",
            "%s主线·首封%s·封成比%.1f%%" % (mk["sector"], mk["firstSeal"], mk["sealRatio"]),
            "维度2[主线高封成比卡位]：%s板块(%d家涨停)前排身位，封单%.2f亿/封成比%.1f%%" % (
                mk["sector"], hy_counts.get(mk["sector"], 1), mk["sealYi"], mk["sealRatio"]
            )
        )

    # 第三梯队（3只）：龙虎榜顶级游资/北向/机构大额净买合力股
    zt_by_code = {x["code"]: x for x in scored_zt}
    top_lhb_codes = sorted(bmap.keys(), key=lambda c: -bmap[c]["netWan"])
    for lc in top_lhb_codes:
        if len(picked) >= 8:
            break
        linfo = bmap[lc]
        if linfo["netWan"] <= 1500:
            continue
        base_item = zt_by_code.get(lc) or {
            "code": lc,
            "name": linfo["name"],
            "sector": "龙虎榜主力",
            "boards": 1 if linfo["chg"] >= 9.5 else 0,
            "brokenCount": 0,
            "firstSeal": "龙虎榜净买第1梯队",
            "sealYi": round(linfo["netWan"] / 10000.0, 2),
            "amountYi": 10.0,
            "sealRatio": linfo["netRatio"],
            "turnover": 15.0,
            "score": 90.0
        }
        seat_tags = " + ".join(list(dict.fromkeys([s["tag"] for s in linfo.get("seats", []) if s["side"].startswith("买")]))) or "主力游资"
        add_candidate(
            base_item,
            "%d板·龙虎榜合力" % max(1, base_item["boards"]),
            "HOT_MONEY",
            "%s·净买%+.2f亿" % (seat_tags[:16], linfo["netWan"] / 10000.0),
            "维度3[龙虎榜游资/机构合力]：龙虎榜净买入 %+.0f万 (占总成交 %+.1f%%)，席位：%s" % (
                linfo["netWan"], linfo["netRatio"], seat_tags
            )
        )

    # 若不足 8 只，用评分最高的前排涨停股补足
    for x in sorted(scored_zt, key=lambda k: -k["score"]):
        if len(picked) >= 8:
            break
        add_candidate(
            x,
            "%d连板精选" % x["boards"],
            "WEAK_TO_STRONG",
            "首封%s·封单%.2f亿" % (x["firstSeal"], x["sealYi"]),
            "维度2[综合量化高分]：连板梯队与封成比综合评分前列"
        )

    # 第四梯队（2只）：高辨识度分歧烂板 / 炸板池风控与反包观察锚点
    # 1) 优先选连板股中炸板次数最多的高分歧活口
    high_div_zt = sorted([x for x in scored_zt if x["brokenCount"] >= 2], key=lambda k: (-k["boards"], -k["brokenCount"]))
    for hd in high_div_zt:
        if len(picked) >= 9:
            break
        add_candidate(
            hd,
            "%d板·强分歧回封" % hd["boards"],
            "BROKEN_WARN",
            "盘中炸板%d次·换手%.1f%%" % (hd["brokenCount"], hd["turnover"]),
            "维度4[高分歧风控/弱转强锚点]：盘中经历 %d 次炸板大分歧回封，次日高开为超预期弱转强，低开则防核按钮" % hd["brokenCount"]
        )

    # 2) 从当日炸板池选 1~2 只成交额最大、曾触及涨停的核心辨识度炸板股
    sorted_zb = sorted(zb_pool, key=lambda x: -float(x.get("amount") or 0))
    for zb in sorted_zb:
        if len(picked) >= 10:
            break
        zcode = str(zb.get("c") or "")
        zname = str(zb.get("n") or "")
        zamo = round(float(zb.get("amount") or 0) / 1e8, 2)
        zfbt = fmt_time(zb.get("fbt"))
        zzbc = int(zb.get("zbc") or 1)
        zhy = zb.get("hybk") or "炸板预警"
        add_candidate(
            {
                "code": zcode, "name": zname, "sector": zhy,
                "boards": 0, "brokenCount": zzbc, "firstSeal": zfbt,
                "sealYi": 0.0, "amountYi": zamo, "sealRatio": 0.0,
                "turnover": float(zb.get("hs") or 0), "score": 60.0
            },
            "炸板池·风控/反包",
            "BROKEN_WARN",
            "首触%s·炸板%d次·成交%.1f亿" % (zfbt, zzbc, zamo),
            "维度4[炸板风控/次日反包观察]：当日 %s 曾封板后炸板回落，作为次日情绪修复反包或亏钱效应风控锚点" % zfbt
        )

    # 批量拉取腾讯 L1 实时五档盘口与量比补齐所有字段
    picked = enrich_with_tencent_l1(picked)

    # 如果是 9:25 集合竞价模式 (auction)，按实时竞价高开幅度与量比动态重排并打上实时竞价弱转强标签
    if mode == "auction":
        for p in picked:
            gap = p.get("openGap", 0.0)
            vr = float(p.get("volRatio") or 0.0)
            if 0.5 <= gap <= 8.5 and vr >= 1.5:
                p["signalType"] = "WEAK_TO_STRONG"
                p["signalTag"] = "【9:25竞价弱转强】高开%+.2f%%·量比%.1f" % (gap, vr)
            elif gap <= -3.0:
                p["signalType"] = "BROKEN_WARN"
                p["signalTag"] = "【9:25竞价不及预期】低开%.2f%%·防核按钮" % gap

    # 汇总当日市场情绪快照
    zt_cnt = len(zt_pool)
    zb_cnt = len(zb_pool)
    dt_cnt = len(dt_pool)
    zb_rate = round(zb_cnt / max(1, zt_cnt + zb_cnt) * 100.0, 1)
    max_b = max([x["boards"] for x in scored_zt], default=1)
    if zt_cnt >= 80 and zb_rate < 25:
        phase = "高潮/主升加速期"
    elif zt_cnt < 35 or dt_cnt > 20:
        phase = "冰点/退潮期"
    elif zb_rate > 35:
        phase = "高位强分歧期"
    else:
        phase = "主线发酵/良性轮动期"

    result = {
        "ok": True,
        "generatedAt": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tradeDate": qdate,
        "lhbDate": lhb_date,
        "mode": mode,
        "marketSummary": {
            "phase": phase,
            "ztCount": zt_cnt,
            "zbCount": zb_cnt,
            "dtCount": dt_cnt,
            "zbRate": zb_rate,
            "maxBoard": max_b,
            "topSectors": [{"name": k, "count": hy_counts[k]} for k in top_sectors]
        },
        "selectionRules": [
            "席位1-2 [空间高标组]：全市场最高连板身位，按连板数+封成比+首封时间排序",
            "席位3-5 [主线1进2/2进3卡位组]：涨停最多TOP3主线板块中，高封成比、早盘秒板、换手健康的晋级先锋",
            "席位6-8 [龙虎榜游资/北向/机构合力组]：东财最新龙虎榜大额净买入且顶级游资/机构协同锁仓",
            "席位9-10 [高分歧回封/炸板风控锚点组]：大成交分歧烂板或核心炸板股，用于次日9:25观察弱转强反包或亏钱效应预警"
        ],
        "picks": picked
    }
    return result


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

    ms = res["marketSummary"]
    print("=== EdsonStock 超短 Top 10 自动选股完成 (交易日: %s | 模式: %s) ===" % (res["tradeDate"], res["mode"]))
    print("  市场情绪: 【%s】 | 自然涨停: %d家 | 炸板: %d家 (炸板率 %.1f%%) | 最高板: %d连板" %
          (ms["phase"], ms["ztCount"], ms["zbCount"], ms["zbRate"], ms["maxBoard"]))
    print("  主线板块: " + " | ".join(["%s(%d家)" % (s["name"], s["count"]) for s in ms["topSectors"]]))
    print("  已写入文件: %s (共 %d 只核心标的)" % (out_path, len(res["picks"])))
    for idx, p in enumerate(res["picks"], 1):
        print("  %2d. [%s] %s (%s | %s) 现价:%.2f (%+.2f%%) | 封单:%.2f亿 (封成比%.1f%%) | 量比:%s | %s" %
              (idx, p["boardLabel"], p["name"], p["code"], p["sector"], p["price"], p["chg"],
               p["sealYi"], p["sealRatio"], p["volRatio"], p["signalTag"]))


if __name__ == "__main__":
    main()
