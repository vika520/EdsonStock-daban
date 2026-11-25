#!/usr/bin/env python3
"""A股盘中实时五档盘口、超短打板情绪雷达与龙虎榜席位拆解引擎（零依赖 Python 3.8+ 标准库）。

核心能力：
  1) quote     : 盘中实时五档买卖盘口、量比、换手率、内外盘主动差、涨跌停价、封成比（腾讯 L1 + 新浪 L1 双源自动降级）
  2) kline     : 个股/指数多周期前复权/不复权 K 线（daily / weekly / m60 / m30 / m15 / m5，腾讯接口，自带 MA/ATR/量比）
  3) radar     : 超短情绪周期温度计（涨停池/跌停池/炸板池、连板梯队、1进2/2进3晋级率、封成比排名、情绪阶段判定）
                 支持东财当日实时池（curl 防封）+ 同花顺历史池（limit=200）双通道
  4) billboard : 龙虎榜个股与全市场买卖前五席位穿透 + 顶级游资/机构/高频量化/拉萨天团标签自动归类

示例：
  python3 scripts/realtime_and_short.py --action quote --codes 600519,000001,300750
  python3 scripts/realtime_and_short.py --action kline --code 600519 --period day --count 30
  python3 scripts/realtime_and_short.py --action radar
  python3 scripts/realtime_and_short.py --action billboard --top 15
  python3 scripts/realtime_and_short.py --action billboard --code 600519
"""
import argparse
import datetime as dt
import json
import re
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# 游资 / 量化 / 散户席位关键词字典
SEAT_TAGS = [
    # ---- 2026 主流量化 / 外资 / 合并后券商席位（放在最前，优先级高于旧关键词）----
    # 国泰君安 + 海通证券合并为「国泰海通」，旧关键词「国泰君安证券股份有限公司总部」匹配不到合并后名称
    ("国泰海通证券股份有限公司总部", "量化(国泰海通总部)"),
    ("国泰海通证券股份有限公司上海松江区中山东路", "游资(松江中山东路)"),
    ("高盛(中国)证券有限责任公司上海世纪大道", "量化/外资(高盛世纪大道)"),
    ("高盛证券有限责任公司上海世纪大道", "量化/外资(高盛世纪大道)"),
    ("摩根大通证券(中国)有限公司上海银城中路", "量化/外资(摩根大通银城中路)"),
    ("摩根士丹利证券(中国)有限公司上海国际金融中心", "量化/外资(大摩IFC)"),
    ("中金公司", "量化(中金)"),
    ("华泰证券股份有限公司总部", "量化(华泰总部)"),
    ("中信建投证券股份有限公司总部", "量化(中信建投总部)"),
    ("招商证券股份有限公司总部", "量化(招商总部)"),
    ("东方财富证券股份有限公司拉萨", "散户(拉萨天团/东财)"),
    ("国泰海通证券股份有限公司上海长宁区江苏路", "游资(章盟主)"),
    ("国泰海通证券股份有限公司上海浦东新区银城中路", "游资(银城中路)"),
    ("国泰海通证券股份有限公司北京中关村南大街", "游资(中关村)"),
    # ---- 原有席位 ----
    ("机构专用", "机构"),
    ("沪股通", "北向"),
    ("深股通", "北向"),
    ("拉萨", "散户(拉萨天团)"),
    ("华鑫证券有限责任公司上海分公司", "量化(华鑫上海)"),
    ("中国国际金融股份有限公司上海分公司", "量化(中金上海)"),
    ("瑞银证券有限责任公司上海花园石桥路", "量化/外资(瑞银)"),
    ("国泰君安证券股份有限公司总部", "量化(国君总部)"),
    ("中信证券股份有限公司总部", "量化/自营(中信总部)"),
    ("中信证券股份有限公司上海分公司", "量化(中信上海)"),
    ("福州六一中路", "游资(六一中路)"),
    ("天津东丽开发区二纬路", "游资(六一中路关联)"),
    ("北京呼家楼", "游资(呼家楼)"),
    ("北京朝外大街", "游资(呼家楼关联)"),
    ("北京东三环中路", "游资(呼家楼关联)"),
    ("江苏路", "游资(章盟主)"),
    ("西安西大街", "游资(方新侠关联)"),
    ("宁波和源路", "游资(宁波敢死队/和源路)"),
    ("杭州延安路", "游资(章盟主)"),
    ("西安朱雀大街", "游资(方新侠)"),
    ("兴业证券股份有限公司陕西分公司", "游资(方新侠)"),
    ("南京太平南路", "游资(作手新一)"),
    ("大连黄河路", "游资(陈小群)"),
    ("大连金马路", "游资(陈小群)"),
    ("宜昌沿江大道", "游资(消闲派)"),
    ("杭州上塘路", "游资(上塘路/砸盘王)"),
    ("宁波桑田路", "游资(桑田路)"),
    ("深圳益田路荣超商务中心", "游资(益田路)"),
    ("上海溧阳路", "游资(溧阳路)"),
    ("绍兴人民中路", "游资(赵老哥)"),
]


def tag_seat(name):
    if not name:
        return "普通营业部"
    for kw, tag in SEAT_TAGS:
        if kw in name:
            return tag
    return "普通营业部"


def normalize_code(raw):
    """将任意股票代码归一化为 (pure_6digit, prefix_code, board_name, limit_pct)。"""
    s = str(raw).strip().lower()
    m = re.search(r"(\d{6})", s)
    if not m:
        raise ValueError("无效的股票代码: %s" % raw)
    code = m.group(1)
    if s.startswith(("sh", "sz", "bj")):
        prefix = s[:2]
    elif code.startswith(("60", "68", "51", "58")):
        prefix = "sh"
    elif code.startswith(("00", "30", "15")):
        prefix = "sz"
    elif code.startswith(("8", "4", "92")):
        prefix = "bj"
    else:
        prefix = "sh"

    if code.startswith("688") or code.startswith("689"):
        board, limit_pct = "科创板", 0.20
    elif code.startswith("300") or code.startswith("301"):
        board, limit_pct = "创业板", 0.20
    elif prefix == "bj" or code.startswith(("8", "4", "92")):
        board, limit_pct = "北交所", 0.30
    else:
        board, limit_pct = "主板", 0.10
    return code, prefix + code, board, limit_pct


def http_get(url, headers=None, use_curl=False, encoding="utf-8"):
    """通用 HTTP 请求：支持东财防封 curl 子进程降级。"""
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    if not use_curl:
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=12, context=CTX) as r:
                return r.read().decode(encoding, "ignore")
        except Exception:
            pass
    cmd = ["curl", "-s", "-L", "--max-time", "15", "-A", hdrs["User-Agent"]]
    for k, v in hdrs.items():
        if k.lower() != "user-agent":
            cmd.extend(["-H", "%s: %s" % (k, v)])
    cmd.append(url)
    out = subprocess.check_output(cmd)
    return out.decode(encoding, "ignore")


def action_quote(codes_str):
    """获取腾讯 L1 实时五档盘口、内外盘、量比、换手率、涨跌停价与封单比。"""
    raw_list = [c.strip() for c in codes_str.split(",") if c.strip()]
    norm_list = [normalize_code(c) for c in raw_list]
    q_param = ",".join([x[1] for x in norm_list])
    url = "https://qt.gtimg.cn/q=" + q_param
    text = http_get(url, encoding="gbk")

    print("=== 盘中实时五档盘口与微观量价快照（数据源：腾讯 L1 实时行情 [真实公开数据]）===")
    for line in text.strip().splitlines():
        if "~" not in line:
            continue
        parts = line.split("~")
        if len(parts) < 48:
            continue
        name = parts[1]
        code = parts[2]
        now_px = float(parts[3] or 0)
        pre_close = float(parts[4] or 0)
        open_px = float(parts[5] or 0)
        vol_hand = float(parts[6] or 0)        # 成交量（手）
        outer_vol = float(parts[7] or 0)       # 外盘（主动买，手）
        inner_vol = float(parts[8] or 0)       # 内盘（主动卖，手）
        high_px = float(parts[33] or 0)
        low_px = float(parts[34] or 0)
        chg_pct = float(parts[32] or 0)
        amount_wan = float(parts[37] or 0)     # 成交额（万元）
        turnover = float(parts[38] or 0)       # 换手率（%）
        pe_ttm = parts[39]
        circ_mv = float(parts[44] or 0)        # 流通市值（亿元）
        total_mv = float(parts[45] or 0)       # 总市值（亿元）
        limit_up = float(parts[47] or 0)
        limit_down = float(parts[48] or 0)
        vol_ratio = parts[49] if len(parts) > 49 else "-"

        # 五档买盘 (price, vol_hand)
        bids = [(float(parts[9 + i * 2] or 0), int(float(parts[10 + i * 2] or 0))) for i in range(5)]
        # 五档卖盘
        asks = [(float(parts[19 + i * 2] or 0), int(float(parts[20 + i * 2] or 0))) for i in range(5)]

        bid_total = sum(v for _, v in bids)
        ask_total = sum(v for _, v in asks)
        weibi = round((bid_total - ask_total) / max(1, bid_total + ask_total) * 100, 2)
        active_diff = int(outer_vol - inner_vol)

        # 封成比 / 封流比（若处于涨停价附近）
        seal_info = ""
        if limit_up > 0 and abs(now_px - limit_up) <= 0.01 and bids[0][1] > 0:
            seal_amount_wan = bids[0][0] * bids[0][1] * 100 / 10000.0
            seal_to_amt = round(seal_amount_wan / max(1.0, amount_wan) * 100, 2)
            seal_to_mv = round((seal_amount_wan / 10000.0) / max(0.01, circ_mv) * 100, 2)
            seal_info = " | 【涨停封单】%.0f万 (封成比 %.2f%% / 封流比 %.2f%%)" % (
                seal_amount_wan, seal_to_amt, seal_to_mv
            )

        print("\n[%s %s] 现价: %.2f (%+.2f%%) | 开: %.2f 高: %.2f 低: %.2f 昨收: %.2f" %
              (code, name, now_px, chg_pct, open_px, high_px, low_px, pre_close))
        print("  成交额: %.2f亿 | 换手率: %.2f%% | 量比: %s | 流通市值: %.2f亿 (总 %.2f亿) | PE-TTM: %s" %
              (amount_wan / 10000.0, turnover, vol_ratio, circ_mv, total_mv, pe_ttm))
        print("  涨停价: %.2f | 跌停价: %.2f | 外盘(主动买): %d手 vs 内盘(主动卖): %d手 (净差 %+d手) | 五档委比: %+.2f%%%s" %
              (limit_up, limit_down, int(outer_vol), int(inner_vol), active_diff, weibi, seal_info))
        print("  卖五~卖一: " + " | ".join(["卖%d %.2f(%d手)" % (5 - i, asks[4 - i][0], asks[4 - i][1]) for i in range(5)]))
        print("  买一~买五: " + " | ".join(["买%d %.2f(%d手)" % (i + 1, bids[i][0], bids[i][1]) for i in range(5)]))


def action_kline(code_raw, period="day", count=30, fq="qfq"):
    """获取腾讯多周期 K 线（支持 day/week/m60/m30/m15/m5）并计算均线与 ATR。"""
    _, pcode, board, limit_pct = normalize_code(code_raw)
    if period in ("m5", "m15", "m30", "m60"):
        url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=%s,%s,,%d" % (pcode, period, count)
    else:
        fq_tag = "qfq" if fq == "qfq" else ""
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,%s,,,%d,%s" % (pcode, period, count, fq_tag)
    raw = json.loads(http_get(url))
    data_node = (raw.get("data") or {}).get(pcode) or {}
    key = "%s%s" % (fq if fq else "", period)
    rows = data_node.get(key) or data_node.get(period) or data_node.get("qfq" + period) or []
    print("=== %s (%s, 涨跌停 %.0f%%) %s K线 (最近 %d 根, 口径: 腾讯行情 [真实公开数据]) ===" %
          (pcode, board, limit_pct * 100, period, len(rows)))
    closes = []
    for r in rows:
        closes.append(float(r[2]))
    for i, r in enumerate(rows[-min(len(rows), count):]):
        idx = len(rows) - min(len(rows), count) + i
        c = float(r[2])
        o = float(r[1])
        h = float(r[3])
        l = float(r[4])
        v = float(r[5])
        prev_c = closes[idx - 1] if idx > 0 else o
        chg = (c / prev_c - 1) * 100 if prev_c > 0 else 0.0
        ma5 = sum(closes[max(0, idx - 4):idx + 1]) / min(5, idx + 1)
        ma20 = sum(closes[max(0, idx - 19):idx + 1]) / min(20, idx + 1)
        print("  %s | 开:%7.2f 高:%7.2f 低:%7.2f 收:%7.2f (%+6.2f%%) | 量:%10.0f | MA5:%7.2f MA20:%7.2f" %
              (r[0], o, h, l, c, chg, v, ma5, ma20))


def fetch_em_pool(pool_name, date_str):
    """使用 curl 子进程拉取东财当日涨停/跌停/炸板池（getTopicZTPool / getTopicDTPool / getTopicZBPool）。

    排序参数按池区分（2026-11-25 审计修复）：跌停池元素没有 fbt（首封时间）字段，
    传 sort=fbt:asc 服务端返回 tc=43 但 pool=[]（跌停明细全部丢失，dtCount 恒为 0）。
    跌停池必须用 sort=fund:asc；涨停/炸板池保持 fbt:asc。
    """
    sort_field = "fund:asc" if pool_name == "getTopicDTPool" else "fbt:asc"
    url = ("https://push2ex.eastmoney.com/%s?ut=7eea3edcaed734bea9cbfc24409ed989"
           "&dpt=wz.ztzt&Pageindex=0&Pagesize=500&sort=%s&date=%s" % (pool_name, sort_field, date_str))
    try:
        text = http_get(url, headers={"Referer": "https://quote.eastmoney.com/ztb/"}, use_curl=True)
        d = json.loads(text)
        return (d.get("data") or {})
    except Exception:
        return {}


def action_radar(date_str=None):
    """超短打板情绪周期雷达：结合东财当日三池 + EdsonStock 历史晋级率。"""
    if not date_str:
        date_str = dt.datetime.now().strftime("%Y%m%d")
    zt_data = fetch_em_pool("getTopicZTPool", date_str)
    zb_data = fetch_em_pool("getTopicZBPool", date_str)
    dt_data = fetch_em_pool("getTopicDTPool", date_str)

    zt_pool = zt_data.get("pool") or []
    zb_pool = zb_data.get("pool") or []
    dt_pool = dt_data.get("pool") or []
    qdate = zt_data.get("qdate") or date_str

    # 若东财当日池为空（如周末/非交易时段），自动回退读取 EdsonStock extras.json & history.json
    if not zt_pool:
        print("提示：东财实时涨停池当前为空或休市，自动切换至 EdsonStock 最新收盘结构快照 (extras.json + history.json)...")
        try:
            from query import load
            ext, _ = load("sentiment", True)
            hist, _ = load("history", True)
            snap = (ext.get("sentiment") or {}).get("snapshot") or {}
            print("=== A股超短情绪快照 (asof %s, 口径: 东财收盘池 + 同花顺历史池 [真实公开数据]) ===" %
                  ((ext.get("meta") or {}).get("asof")))
            print("  涨停家数: %s | 跌停家数: %s | 炸板家数: %s | 炸板率: %s%% | 最高连板: %s板 | 晋级率: %s%%" %
                  (snap.get("涨停"), snap.get("跌停"), snap.get("炸板"), snap.get("炸板率"), snap.get("最高板"), snap.get("晋级率")))
            hrows = ((hist.get("sentiment") or {}).get("rows") or [])[-10:]
            print("\n  最近 10 个交易日情绪周期序列 [日期, 涨停数, 最高板, 连板家数, 连板晋级率%]：")
            for r in hrows:
                print("   ", r)
            return
        except Exception as e:
            print("回退读取失败:", e)
            return

    # 剔除 ST 统计自然涨停
    non_st_zt = [x for x in zt_pool if "ST" not in str(x.get("n", ""))]
    zt_count = len(non_st_zt)
    zb_count = len(zb_pool)
    dt_count = len(dt_pool)
    zb_rate = round(zb_count / max(1, zt_count + zb_count) * 100, 2)

    # 连板梯队统计
    lbc_map = {}
    for x in non_st_zt:
        lb = int(x.get("lbc") or 1)
        lbc_map.setdefault(lb, []).append(x)
    max_lb = max(lbc_map.keys()) if lbc_map else 0

    # 定量情绪周期判定
    if zt_count < 30 or dt_count > 25 or max_lb <= 3:
        phase = "冰点/退潮期（严控仓位，谨防高位核按钮，等待冰点试错信号）"
    elif zt_count > 85 and zb_rate < 25 and max_lb >= 6:
        phase = "高潮/主升加速期（持筹盛宴，后排缩量谨防次日一致性过高分歧兑现）"
    elif zb_rate > 38 or (dt_count >= 12 and max_lb >= 5):
        phase = "高低切换/强分歧期（去弱留强，关注抗分歧回封核心，规避中位跟风断板）"
    else:
        phase = "良性轮动/发酵期（聚焦主线前排1进2、2进3卡位及高封成比龙头）"

    print("=== A股超短打板与情绪周期雷达 (交易日: %s, 数据源: 东财实时三池 [真实公开数据]) ===" % qdate)
    print("  自然涨停(非ST): %d 家 | 炸板: %d 家 | 跌停: %d 家 | 炸板率: %.2f%% | 最高空间板: %d 连板" %
          (zt_count, zb_count, dt_count, zb_rate, max_lb))
    print("  当前情绪周期判定: 【%s】" % phase)

    print("\n--- 连板身位梯队分布 ---")
    for lb in sorted(lbc_map.keys(), reverse=True):
        items = lbc_map[lb]
        if lb == 1:
            print("  [首板] 共 %d 家" % len(items))
            continue
        desc_list = []
        for it in sorted(items, key=lambda k: -float(k.get("fund") or 0)):
            code = it.get("c")
            name = it.get("n")
            hy = it.get("hybk") or "-"
            fbt = str(it.get("fbt") or "").zfill(6)
            fbt_fmt = "%s:%s:%s" % (fbt[:2], fbt[2:4], fbt[4:6]) if len(fbt) == 6 else fbt
            fund_yi = float(it.get("fund") or 0) / 1e8
            amo_yi = float(it.get("amount") or 1) / 1e8
            seal_ratio = round(fund_yi / max(0.01, amo_yi) * 100, 1)
            zbc = int(it.get("zbc") or 0)
            desc_list.append("%s(%s|%s|首封%s|封单%.2f亿/封成比%.1f%%|炸%d次)" %
                             (name, code, hy, fbt_fmt, fund_yi, seal_ratio, zbc))
        print("  [%d连板] (%d家): %s" % (lb, len(items), " ； ".join(desc_list)))

    # 行业板块涨停集中度
    hy_count = {}
    for x in non_st_zt:
        hy = x.get("hybk") or "未分类"
        hy_count[hy] = hy_count.get(hy, 0) + 1
    top_hy = sorted(hy_count.items(), key=lambda kv: -kv[1])[:8]
    print("\n--- 当日涨停主线行业集中度 TOP 8 ---")
    print("  " + " | ".join(["%s: %d家" % (k, v) for k, v in top_hy]))


def action_billboard(code_filter=None, top_n=15):
    """拉取东财龙虎榜列表及营业部买卖前五席位明细，自动打上游资/机构/量化/散户标签。"""
    base = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    if code_filter:
        code, _, _, _ = normalize_code(code_filter)
        flt = '(SECURITY_CODE="%s")' % code
    else:
        flt = ""
    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,CLOSE_PRICE,CHANGE_RATE,BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,ACCUM_AMOUNT,EXPLANATION",
        "pageNumber": "1",
        "pageSize": str(max(20, top_n * 2)),
        "sortTypes": "-1,-1",
        "sortColumns": "TRADE_DATE,BILLBOARD_NET_AMT",
        "source": "WEB",
        "client": "WEB",
    }
    if flt:
        params["filter"] = flt
    url = base + "?" + urllib.parse.urlencode(params)
    raw = json.loads(http_get(url, use_curl=True))
    rows = ((raw.get("result") or {}).get("data")) or []
    if not rows:
        print("未查询到龙虎榜记录。")
        return

    latest_date = rows[0].get("TRADE_DATE", "")[:10]
    if not code_filter:
        rows = [r for r in rows if str(r.get("TRADE_DATE", "")).startswith(latest_date)]

    # 按股票代码去重合并同日多条上榜原因
    seen = {}
    for r in rows:
        c = r.get("SECURITY_CODE")
        dt_s = str(r.get("TRADE_DATE", ""))[:10]
        key = (c, dt_s)
        if key not in seen:
            seen[key] = r
    uniq_rows = list(seen.values())[:top_n]

    print("=== 龙虎榜主力净买与游资/量化席位穿透 (最新披露日: %s, 口径: 东财公告 [真实公开数据]) ===" % latest_date)
    for r in uniq_rows:
        c = r.get("SECURITY_CODE")
        name = r.get("SECURITY_NAME_ABBR")
        tdate = str(r.get("TRADE_DATE", ""))[:10]
        chg = float(r.get("CHANGE_RATE") or 0)
        net_wan = float(r.get("BILLBOARD_NET_AMT") or 0) / 1e4
        buy_wan = float(r.get("BILLBOARD_BUY_AMT") or 0) / 1e4
        sell_wan = float(r.get("BILLBOARD_SELL_AMT") or 0) / 1e4
        tot_amo_wan = float(r.get("ACCUM_AMOUNT") or 1) / 1e4
        net_ratio = round(net_wan / max(1.0, tot_amo_wan) * 100, 2)
        reason = r.get("EXPLANATION") or ""

        print("\n[%s %s] (%s) 涨跌幅: %+.2f%% | 龙虎榜净买: %+.0f万 (买 %.0f万 / 卖 %.0f万 | 占总成交 %+.2f%%)" %
              (c, name, tdate, chg, net_wan, buy_wan, sell_wan, net_ratio))
        print("  上榜原因: %s" % reason)

        # 查询该股当日买卖前五营业部明细
        for rpt, label in [("RPT_BILLBOARD_DAILYDETAILSBUY", "买方TOP5"), ("RPT_BILLBOARD_DAILYDETAILSSELL", "卖方TOP5")]:
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
            u_seat = base + "?" + urllib.parse.urlencode(p_seat)
            try:
                s_raw = json.loads(http_get(u_seat, use_curl=True))
                s_rows = ((s_raw.get("result") or {}).get("data")) or []
                seat_strs = []
                seen_dept = set()
                for sr in s_rows:
                    dept = sr.get("OPERATEDEPT_NAME") or "未知席位"
                    b_w = float(sr.get("BUY") or 0) / 1e4
                    s_w = float(sr.get("SELL") or 0) / 1e4
                    sig = (dept, round(b_w), round(s_w))
                    if sig in seen_dept:
                        continue
                    seen_dept.add(sig)
                    n_w = float(sr.get("NET") or (b_w * 1e4 - s_w * 1e4)) / 1e4
                    tag = tag_seat(dept)
                    short_dept = dept.replace("证券股份有限公司", "").replace("有限责任公司", "").replace("证券营业部", "")
                    seat_strs.append("[%s] %s(买%.0f万/卖%.0f万/净%+.0f万)" % (tag, short_dept, b_w, s_w, n_w))
                    if len(seat_strs) >= 5:
                        break
                if seat_strs:
                    print("  %s: %s" % (label, " ； ".join(seat_strs)))
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description="A股盘中五档盘口、超短情绪雷达与龙虎榜席位拆解")
    ap.add_argument("--action", required=True, choices=["quote", "kline", "radar", "billboard"],
                    help="quote: 五档实时盘口 | kline: 多周期K线 | radar: 超短打板情绪雷达 | billboard: 龙虎榜席位拆解")
    ap.add_argument("--codes", default="600519", help="逗号分隔的股票代码（用于 quote）")
    ap.add_argument("--code", default=None, help="单只股票代码（用于 kline 或 billboard 过滤）")
    ap.add_argument("--period", default="day", help="K线周期: day / week / m60 / m30 / m15 / m5")
    ap.add_argument("--count", type=int, default=30, help="K线条数")
    ap.add_argument("--fq", default="qfq", choices=["qfq", "hfq", "none"], help="复权方式")
    ap.add_argument("--date", default=None, help="指定日期 YYYYMMDD（用于 radar）")
    ap.add_argument("--top", type=int, default=10, help="龙虎榜显示条数")
    args = ap.parse_args()

    if args.action == "quote":
        action_quote(args.codes if not args.code else args.code)
    elif args.action == "kline":
        action_kline(args.code or args.codes.split(",")[0], args.period, args.count, "" if args.fq == "none" else args.fq)
    elif args.action == "radar":
        action_radar(args.date)
    elif args.action == "billboard":
        action_billboard(args.code, args.top)


if __name__ == "__main__":
    main()
