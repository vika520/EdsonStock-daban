#!/usr/bin/env python3
"""基本面数据与排雷模块（零依赖 Python 3.8+ 标准库）— 对应 references/swing_design.md 的 S2。

数据源：东方财富数据中心 datacenter-web（本机实测可用，与被封的 push2 板块接口不同）
  RPT_LICO_FN_CPD        业绩报表：营收/营收同比/净利同比/每股收益/每股净资产/每股经营现金流
  RPT_PUBLIC_OP_NEWPREDICT 业绩预告：类型（预增/略减/首亏...）+ 增幅上下限
  RPT_LIFT_STAGE         限售解禁：解禁日期/占总股本比

字段名坑（实测）：日期列是 REPORTDATE（无下划线）；ROEJQ / KCFJCXSYJLR / CCE_ADD / TOTAL_SHARE 均不存在。
ROE 用 BASIC_EPS / BPS 估算并年化（季报累计），经营现金流/净利用 MGJYXJJE / BASIC_EPS。

缓存：按周落盘 data/fundamentals_cache.json（财务季更、预告日更、解禁周更），避免每日全量请求触发限流。

硬否决（Hard Veto）与 Value-Quality 打分（100）见 respective 函数。

用法：
  python3 scripts/fetch_fundamentals.py --codes 600519,000858 --force
"""
import argparse
import datetime as dt
import json
import pathlib
import sys
import urllib.parse

from realtime_and_short import http_get

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
CACHE_FILE = BASE_DIR / "data" / "fundamentals_cache.json"
CACHE_TTL = {"fin": 7 * 86400, "predict": 86400, "lift": 7 * 86400}  # 秒
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 业绩预告负面类型（一票否决）
NEGATIVE_PREDICT = {"预减", "略减", "首亏", "续亏", "不确定", "减亏"}


def now_beijing():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=8)


def _f(v, default=None):
    try:
        x = float(v)
        return x
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 缓存

def _load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(c):
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print("[fundamentals] 缓存写入失败: %s" % e, file=sys.stderr)


def _cached(kind, code):
    c = _load_cache()
    ent = (c.get(kind) or {}).get(code)
    if not ent:
        return None
    if now_beijing().timestamp() - ent.get("_ts", 0) > CACHE_TTL[kind]:
        return None
    return ent


def _put(kind, code, data):
    c = _load_cache()
    c.setdefault(kind, {})[code] = dict(data, _ts=now_beijing().timestamp())
    _save_cache(c)


# ---------------------------------------------------------------- 东财拉取

def _dc_query(report, columns, extra_params=None, page_size=50):
    params = {"reportName": report, "columns": columns, "pageNumber": "1",
              "pageSize": str(page_size), "source": "WEB", "client": "WEB"}
    if extra_params:
        params.update(extra_params)
    url = DC + "?" + urllib.parse.urlencode(params)
    try:
        raw = json.loads(http_get(url, use_curl=True))
    except Exception as e:
        return [], "请求失败: %s" % str(e)[:60]
    if not raw.get("result"):
        return [], raw.get("message") or "无数据"
    return ((raw.get("result") or {}).get("data")) or [], None


def _latest_report_date():
    """业绩预告对应的最近报告期：最近一个「已经过去」的季末（预告是提前发的，所以是上个季末而非上上个）。

    例：今天 11-25 → 最近过去的季末是 9-30（三季报预告期）。
    """
    d = now_beijing().date()
    m = ((d.month - 1) // 3) * 3          # 本季度的起始月（0/3/6/9）
    if m == 0:
        y, mm = d.year - 1, 12
    else:
        y, mm = d.year, m
    dm = 31 if mm == 12 else (30 if mm in (9, 6) else 31)
    return "%04d-%02d-%02d" % (y, mm, dm)


def fetch_financials(code, force=False):
    """业绩报表：营收同比 YSTZ / 净利同比 SJLTZ / EPS / BPS / 每股经营现金流。"""
    if not force:
        hit = _cached("fin", code)
        if hit:
            return {k: v for k, v in hit.items() if k != "_ts"}
    rows, err = _dc_query(
        "RPT_LICO_FN_CPD",
        "SECURITY_CODE,SECURITY_NAME_ABBR,REPORTDATE,TOTAL_OPERATE_INCOME,YSTZ,PARENT_NETPROFIT,SJLTZ,BASIC_EPS,BPS,MGJYXJJE",
        {"filter": '(SECURITY_CODE="%s")' % code, "sortColumns": "REPORTDATE", "sortTypes": "-1"},
        page_size=4,
    )
    out = {"code": code, "ok": False}
    if rows:
        r = rows[0]
        eps, bps, cps = _f(r.get("BASIC_EPS")), _f(r.get("BPS")), _f(r.get("MGJYXJJE"))
        rd = str(r.get("REPORTDATE") or "")[:10]
        # 年化 ROE 估算：季报累计 EPS / BPS，按截止月数年化
        months = int(rd[5:7]) if len(rd) >= 7 else 12
        ann = 12.0 / months if months > 0 else 1.0
        roe = round(eps / bps * 100 * ann, 2) if (eps is not None and bps and bps > 0) else None
        # 经营现金流 / 净利（每股口径比值等价于总额比值）
        cf_to_np = round(cps / eps, 2) if (cps is not None and eps and eps > 0) else None
        out.update({
            "ok": True, "name": r.get("SECURITY_NAME_ABBR"), "reportDate": rd,
            "revYoy": _f(r.get("YSTZ")), "npYoy": _f(r.get("SJLTZ")),
            "eps": eps, "bps": bps, "roeTtmEst": roe, "cfToNp": cf_to_np,
            "npRaw": _f(r.get("PARENT_NETPROFIT")),
            "dataTier": "真实公开数据（东财业绩报表）；ROE 为 EPS/BPS 年化估算，非官方 ROE",
        })
    else:
        out["error"] = err
    if out.get("ok"):
        _put("fin", code, out)
    return out


def fetch_predict(code, force=False):
    """业绩预告：类型 + 增幅上下限。

    东财坑：同一条预告会拆成多行（营业收入 / 归母净利 / 扣非净利），PREDICT_TYPE 各不同。
    排雷必须看「归母净利润」那一行，不能取 rows[0]（可能是营收行）。
    另外必须校验 reportDate 是最近报告期——否则两年前的旧预告会误杀今天的票。
    """
    if not force:
        hit = _cached("predict", code)
        if hit:
            return {k: v for k, v in hit.items() if k != "_ts"}
    rows, err = _dc_query(
        "RPT_PUBLIC_OP_NEWPREDICT",
        "SECURITY_CODE,NOTICE_DATE,REPORT_DATE,PREDICT_TYPE,PREDICT_CONTENT,ADD_AMP_LOWER,ADD_AMP_UPPER",
        {"filter": '(SECURITY_CODE="%s")' % code, "sortColumns": "NOTICE_DATE", "sortTypes": "-1"},
        page_size=12,
    )
    out = {"code": code, "ok": False}
    latest_rd = _latest_report_date()
    if rows:
        # 只取最近报告期的行
        rd = str(rows[0].get("REPORT_DATE") or "")[:10]
        same = [r for r in rows if str(r.get("REPORT_DATE") or "")[:10] == rd]
        stale = (rd != latest_rd)
        # 优先归母净利润行；其次扣非；最后任意
        def _pick(kw):
            for r in same:
                if kw in str(r.get("PREDICT_CONTENT") or ""):
                    return r
            return None
        r = _pick("归属于上市公司股东的净利润") or _pick("净利润") or same[0]
        lo, hi = _f(r.get("ADD_AMP_LOWER")), _f(r.get("ADD_AMP_UPPER"))
        ptype = str(r.get("PREDICT_TYPE") or "")
        out.update({
            "ok": True, "noticeDate": str(r.get("NOTICE_DATE") or "")[:10],
            "reportDate": rd, "expectedReportDate": latest_rd, "stale": stale,
            "predictType": ptype, "content": r.get("PREDICT_CONTENT"),
            "ampLower": lo, "ampUpper": hi,
            # 过期预告不参与否决（stale=True 时 negative 强制 False）
            "negative": (ptype in NEGATIVE_PREDICT) and not stale,
            "allTypes": sorted(set(str(x.get("PREDICT_TYPE") or "") for x in same)),
            "dataTier": "真实公开数据（公告口径）%s" % ("；注意：预告期 %s 非最近报告期，已标记 stale 不参与否决" % rd if stale else ""),
        })
    else:
        out["error"] = err or "无预告"
        out["ok"] = True   # 无预告不是错误
        out["predictType"] = None
        out["negative"] = False
    _put("predict", code, out)
    return out


def fetch_unlock_map(days_ahead=20, force=False):
    """未来 N 个自然日内有解禁的股票 -> {code: {ratio, date}}。一次拉全市场，按周缓存。"""
    c = _load_cache()
    ent = c.get("lift_all")
    if not force and ent and now_beijing().timestamp() - ent.get("_ts", 0) < CACHE_TTL["lift"]:
        return ent.get("data") or {}
    today = now_beijing().date()
    end = today + dt.timedelta(days=days_ahead * 7 // 5)   # 交易日 ≈ 自然日 × 7/5
    flt = '(FREE_DATE>=\'%s\')(FREE_DATE<=\'%s\')' % (today.isoformat(), end.isoformat())
    rows, err = _dc_query(
        "RPT_LIFT_STAGE", "SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,FREE_SHARES,TOTAL_RATIO,LIFT_MARKET_CAP",
        {"filter": flt, "sortColumns": "FREE_DATE", "sortTypes": "1"}, page_size=500,
    )
    out = {}
    for r in rows or []:
        code = str(r.get("SECURITY_CODE") or "")
        ratio = _f(r.get("TOTAL_RATIO")) or 0.0
        if code and ratio > 0:
            # 同一股票多笔解禁取最大占比
            prev = out.get(code)
            if not prev or ratio > prev["ratio"]:
                out[code] = {"ratio": round(ratio * 100, 2), "date": str(r.get("FREE_DATE") or "")[:10], "name": r.get("SECURITY_NAME_ABBR")}
    c["lift_all"] = {"_ts": now_beijing().timestamp(), "data": out}
    _save_cache(c)
    return out


# ---------------------------------------------------------------- 排雷与打分

def hard_veto(code, fin, pred, unlock, quote, listed_days=None, recent_boards=None):
    """一票否决。返回 (vetoed: bool, reasons: list[str])。

    quote 需含 circMv(亿)/amount20Yi(20日均成交额亿)。
    listed_days: 上市交易日数（日线根数近似），< 180 否决次新。
    recent_boards: 近 15 日涨停收盘次数，>= 3 否决情绪妖股（波段不接连板情绪票）。
    """
    reasons = []
    name = str((fin or {}).get("name") or "")
    if "ST" in name.upper():
        reasons.append("ST/*ST")
    if listed_days is not None and listed_days < 180:
        reasons.append("上市不足180交易日(约)")
    if recent_boards is not None and recent_boards >= 3:
        reasons.append("近15日%d次涨停(情绪妖股)" % recent_boards)
    circ = _f((quote or {}).get("circMv")) or 0.0
    amt = _f((quote or {}).get("amount20Yi")) or 0.0
    if circ and circ < 40:
        reasons.append("流通市值%.1f亿<40亿" % circ)
    if amt and amt < 2.0:
        reasons.append("20日均成交%.2f亿<2亿" % amt)
    u = (unlock or {}).get(code)
    if u and u["ratio"] > 3.0:
        reasons.append("未来20日解禁%.1f%%>3%%(%s)" % (u["ratio"], u["date"]))
    if (pred or {}).get("negative"):
        reasons.append("业绩预告%s" % (pred or {}).get("predictType"))
    fy, ny = _f((fin or {}).get("revYoy")), _f((fin or {}).get("npYoy"))
    if fy is not None and ny is not None and ny < -20 and fy < 0:
        reasons.append("净利同比%.1f%%且营收同比%.1f%%" % (ny, fy))
    return (len(reasons) > 0), reasons


def value_quality_score(fin, pred, pe_ttm=None, sector_pe_median=None):
    """Value-Quality 打分（100）。返回 (score, parts, notes)。

    口径纪律：区分「公司亏损导致字段无效」与「字段缺失」——
    亏损股（eps<=0 / pe<=0）的相关维度给 0 分，不能拿"免费中性分"；
    只有非亏损但字段确实抓不到时才给 50% 中性分。
    """
    notes = []
    eps = _f((fin or {}).get("eps"))
    losing = eps is not None and eps <= 0
    # --- 盈利质量 35
    q = 0.0
    roe = _f((fin or {}).get("roeTtmEst"))
    if losing:
        notes.append("当期亏损(EPS<=0)：质量与成长维度计 0 分")
        quality = 0.0
        growth = 0.0
    else:
        if roe is None:
            q += 35 * 0.5
            notes.append("ROE缺失(中性给分)")
        else:
            q += 20 * min(1.0, max(0.0, (roe - 8.0) / 7.0))    # 8% 起步，15% 满分
        cfn = _f((fin or {}).get("cfToNp"))
        if cfn is None:
            q += 15 * 0.5
            notes.append("现金流/净利缺失(中性给分)")
        else:
            q += 15 * min(1.0, max(0.0, cfn / 0.8))            # 0.8 满分
        quality = q
        # --- 成长景气 35
        g = 0.0
        ry, ny = _f((fin or {}).get("revYoy")), _f((fin or {}).get("npYoy"))
        if ry is None or ny is None:
            g += 35 * 0.5
            notes.append("增速缺失(中性给分)")
        else:
            g += 15 * min(1.0, max(0.0, ry / 10.0))            # 营收同比 10% 满分
            g += 20 * min(1.0, max(0.0, ny / 15.0))            # 净利同比 15% 满分
        pt = str((pred or {}).get("predictType") or "")
        if pt in ("预增", "略增", "扭亏", "续盈"):
            g += 0  # 已在基数内，不重复加分；仅打标签
            notes.append("预告%s[公告口径]" % pt)
        growth = min(35.0, g)
    # --- 估值性价比 30
    v = 0.0
    if pe_ttm is not None and pe_ttm <= 0:
        v = 0.0                       # 亏损/异常 PE：0 分（不是中性分）
        notes.append("PE<=0(亏损或异常)：估值维度 0 分")
    elif pe_ttm is None:
        v = 30 * 0.5
        notes.append("PE缺失(中性给分)")
    elif sector_pe_median and sector_pe_median > 0:
        rel = pe_ttm / sector_pe_median
        v = 30 * (1.0 - min(1.0, max(0.0, (rel - 0.6) / 1.2)))   # 0.6x 满分，1.8x 零分
    else:
        v = 30 * (1.0 - min(1.0, max(0.0, (pe_ttm - 10) / 60)))  # 无行业中位：10 倍满分，70 倍零分
    valuation = v
    return round(quality + growth + valuation, 1), {
        "quality": round(quality, 1), "growth": round(growth, 1), "valuation": round(valuation, 1)
    }, notes


def main():
    ap = argparse.ArgumentParser(description="基本面数据与排雷模块")
    ap.add_argument("--codes", required=True, help="逗号分隔股票代码")
    ap.add_argument("--force", action="store_true", help="跳过缓存")
    args = ap.parse_args()
    unlock = fetch_unlock_map(force=args.force)
    for code in [c.strip() for c in args.codes.split(",") if c.strip()]:
        fin = fetch_financials(code, args.force)
        pred = fetch_predict(code, args.force)
        print(json.dumps({"fin": fin, "predict": pred, "unlock": unlock.get(code)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
