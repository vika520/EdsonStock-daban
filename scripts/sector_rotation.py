#!/usr/bin/env python3
"""行业/题材轮动引擎（零依赖 Python 3.8+ 标准库）— 对应 references/swing_design.md 的 S2。

数据源：腾讯板块排名接口（东财 push2 板块接口在本机网络下被封，见 swing_design.md 第 0 节）
  https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type=hy|gn&sort_type=price&direct=down&offset=N&count=M
  约束：count 上限 200（超过返回 code=1619377101）；行业 31 个全量；概念共 808 个需分页。

接口自带字段（免去手算）：
  zdf_d5 / zdf_d20 / zdf_d60 / zdf_w52 : 各期涨跌幅 %
  zljlr_d5 / zljlr_d20                : 5/20 日主力净流入（万元）
  zgb   "102/122"                     : 板块内上涨家数/总家数（广度）
  turnover / ltsz / hsl / lb          : 成交额(万) / 流通市值(亿) / 换手率% / 量比

三维评分（满分 100）：
  RS 动量     45% : 5/20 日涨幅减中证800 同期 → 相对强弱；60 日作趋势确认
  资金关注    35% : 5/20 日主力净流入符号与强度（相对流通市值归一化）+ 换手率
  拥挤度护栏  20% : 反向指标——过热扣分；触发熔断则阶段直接标「过热预警」

口径声明（必须随输出一起下发）：
  【真实公开数据】板块涨跌幅、主力净流入、成交额、流通市值、涨跌家数 —— 均为腾讯接口原值
  【量价代理】    拥挤度：接口只给当前快照，无 60 日历史序列，用「换手率 + 5 日涨幅 + 主力净流入拐头」做代理，
                  不是方案原本设计的「60 日成交占比分位」。精度低于设计，已在 risk 字段标注。

用法：
  python3 scripts/sector_rotation.py --type hy --top 10 --out web/sector_rotation.json
  python3 scripts/sector_rotation.py --type all            # 行业 + 概念，各取前 N
"""
import argparse
import datetime as dt
import json
import pathlib
import sys

from realtime_and_short import http_get

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
BENCH_CODE = "sh000906"   # 中证800 作相对强弱基准
PAGE = 200

# 概念板块里的「风格/持股/指数」伪概念：不是可交易主题，波段轮动应剔除。
# 黑名单 + 关键词正则双重过滤——腾讯 808 个概念里这类占相当比例，靠枚举追不完。
PSEUDO_CONCEPTS = {
    "证金持股", "社保重仓", "QFII重仓", "基金重仓", "汇金持股", "券商相关", "信托持股",
    "上证50", "红利股", "高股息100", "保险概念", "白马股", "价值股", "中特估", "茅指数",
    "MSCI中国", "富时罗素", "标普道琼斯", "沪深300", "中证500", "上证180", "深证100",
    "中证100", "科创50", "创业板50", "科创板做市股", "央企改革", "国企改革",
    "昨日涨停", "昨日首板", "昨日连板", "昨日触板", "昨日炸板", "昨日跌停", "昨日强势", "昨日触板",
    "预盈预增", "预亏预减", "高送转", "破净股", "低价股", "百元股", "绩优股", "ST股",
    "参股新股", "举牌", "次新股", "新股与次新股", "注册制次新", "共同富裕示范区",
}
import re as _re
_PSEUDO_PATTERN = _re.compile(
    r"(持股|重仓|概念股指数|指数$|样本股|成份股|成分股|做市|次新|预盈|预亏|昨日|高送转|"
    r"破净|低价股|百元股|绩优|ST股|^ST|举牌|融资融券|标的|沪股通|深股通|港股通|转债标的|"
    r"专精特新|新三板|北交所|REITs|CDR|WiFi|PPP|"
    # 与一级行业高度重合的「XX概念」及风格类（2026-11-25 审计补充）：
    r"银行概念|保险概念|券商概念|超级品牌|行业龙头|大盘股|中盘股|小盘股|微盘股|参股基金|"
    r"出海50|中特估|茅指数|宁组合|白马股|价值股|红利股|高股息|成分股指数|"
    # 持股/上市地/工具类（2026-11-25 二次补充）：
    r"MSCI|AH股|^H股|CDR|回购|再贷款|增持|减持|股权激励|并购重组|混改)"
)
MIN_AMT_SHARE_PCT = 0.05   # 概念成交额占全市场 < 0.05% 视为微概念，无波段价值


def _is_pseudo_concept(name):
    """判断是否为风格/持股/指数类伪概念。"""
    n = str(name or "")
    if n in PSEUDO_CONCEPTS:
        return True
    return bool(_PSEUDO_PATTERN.search(n))


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _parse_zgb(s):
    """'102/122' -> (up, total)；异常返回 (0, 0)。"""
    try:
        a, b = str(s).split("/")
        return int(a), int(b)
    except Exception:
        return 0, 0


def now_beijing():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=8)


def fetch_boards(board_type="hy", max_n=200):
    """拉取板块列表，自动分页（count 上限 200）。返回 list[dict]。"""
    out, offset = [], 0
    while offset < max_n:
        url = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type=%s&sort_type=price&direct=down&offset=%d&count=%d"
               % (board_type, offset, PAGE))
        try:
            raw = json.loads(http_get(url))
        except Exception as e:
            print("[sector_rotation] 拉取失败 offset=%d: %s" % (offset, e), file=sys.stderr)
            break
        if raw.get("code") != 0:
            break
        rows = ((raw.get("data") or {}).get("rank_list")) or []
        if not rows:
            break
        out.extend(rows)
        total = (raw.get("data") or {}).get("total") or 0
        offset += PAGE
        if total and offset >= total:
            break
    return out


def fetch_bench_returns(count=70):
    """中证800 近 5/20/60 日涨幅（%），用腾讯指数日线。失败返回 None，轮动降级为绝对涨幅。"""
    try:
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (BENCH_CODE, count)
        raw = json.loads(http_get(url))
        node = (raw.get("data") or {}).get(BENCH_CODE) or {}
        rows = node.get("qfqday") or node.get("day") or []
        if len(rows) < 61:
            return None
        c = [float(r[2]) for r in rows]
        last = c[-1]
        return {"d5": (last / c[-6] - 1) * 100, "d20": (last / c[-21] - 1) * 100, "d60": (last / c[-61] - 1) * 100}
    except Exception:
        return None


def score_board(b, bench, universe_ltsz_median):
    """单板块三维评分。返回 dict 或 None（数据不足）。"""
    name = b.get("name") or ""
    code = b.get("code") or ""
    if not name or not code:
        return None
    ltsz = _f(b.get("ltsz"))          # 亿
    turnover = _f(b.get("turnover"))  # 万
    hsl = _f(b.get("hsl"))            # %
    d5, d20, d60 = _f(b.get("zdf_d5")), _f(b.get("zdf_d20")), _f(b.get("zdf_d60"))
    f5, f20 = _f(b.get("zljlr_d5")), _f(b.get("zljlr_d20"))   # 万元
    up, total = _parse_zgb(b.get("zgb"))
    if ltsz <= 0:
        return None

    # ---- RS 动量 45 分
    if bench:
        rs5, rs20, rs60 = d5 - bench["d5"], d20 - bench["d20"], d60 - bench["d60"]
    else:
        rs5, rs20, rs60 = d5, d20, d60
    # 分段给分：rs20 为主（权重 0.5），rs5 看加速度（0.25），rs60 看趋势确认（0.25）
    def _band(x, hi, lo):
        # x>=hi 满分，x<=lo 零分，线性
        if x >= hi:
            return 1.0
        if x <= lo:
            return 0.0
        return (x - lo) / (hi - lo)
    rs_score = 45.0 * (0.5 * _band(rs20, 8.0, -6.0) + 0.25 * _band(rs5, 4.0, -3.0) + 0.25 * _band(rs60, 15.0, -10.0))

    # ---- 资金关注 35 分：主力净流入相对流通市值归一化（万元/亿 → 千分位）
    f5_rel = f5 / (ltsz * 100) * 1000    # 千分比 ‰
    f20_rel = f20 / (ltsz * 100) * 1000
    flow_score = 35.0 * (0.5 * _band(f5_rel, 3.0, -3.0) + 0.5 * _band(f20_rel, 8.0, -8.0))
    # RS 门槛：板块显著跑输基准（rs20 < -3）时，资金流入不给满分——
    # 跑输 + 流入往往是「接盘」而非「建仓」，资金分减半，避免 RS 为负的板块靠资金项排第一
    if rs20 < -3.0:
        flow_score *= 0.5
    # 广度加成：上涨占比 > 60% 小幅加分，< 40% 小幅减分（上限 ±5）
    up_ratio = (up / total * 100.0) if total > 0 else 50.0
    breadth_adj = 5.0 * _band(up_ratio, 80.0, 20.0) - 2.5

    # ---- 拥挤度护栏 20 分（反向）：换手越高、5 日涨越多、主力净流入拐头 → 越拥挤
    hot = 0.0
    hot += _band(hsl, 6.0, 1.0) * 0.4          # 换手率
    hot += _band(d5, 12.0, 0.0) * 0.35         # 5 日涨幅
    hot += (1.0 if (f5 > 0 and f20 < 0) else 0.0) * 0.25   # 短期流入但中期流出（情绪脉冲）
    hot = min(1.0, hot)
    crowd_score = 20.0 * (1.0 - hot)
    # 熔断：换手 > 5% 且 5 日涨 > 10%，或 主力 5 日净流入但 20 日净流出且 5 日涨 > 8%
    overheat = (hsl > 5.0 and d5 > 10.0) or (f5 > 0 and f20 < 0 and d5 > 8.0)

    total_score = rs_score + flow_score + breadth_adj + crowd_score

    # ---- 阶段分类（与评分维度保持一致：RS 为主、资金为辅、拥挤度熔断）
    # 2026-11-25 审计修复「假主升期」：中证800 20 日 -6.33% 的弱势里，任何横盘少跌的防御板块天然 rs20 > +5%，
    # 银行/家电/农林牧渔 20 日主力净流出 -58‰ ~ -708‰ 却被标「主升期」。
    # 「主升期」现在必须同时满足：rs20 > 3.0 且 绝对收益 d20 > 0 且 资金不显著流出
    # （f20_rel > -15‰ 或 f5_rel > 0 或上涨占比 >= 55%）。仅抗跌但主力持续派发的归「抗跌防守」。
    if overheat:
        stage = "过热预警"
    elif rs20 <= -3.0 and f20_rel <= 0:
        stage = "退潮期"           # 跑输且资金流出
    elif rs20 > 3.0 and d20 > 0 and (f20_rel > -15.0 or f5_rel > 0 or up_ratio >= 55.0):
        # 真强势：跑赢 + 绝对上涨 + 资金未显著流出
        stage = "启动期" if rs5 >= rs20 * 0.5 else "主升期"
    elif rs20 > 3.0:
        stage = "抗跌防守"         # 仅相对抗跌（大盘跌得更少），主力仍在流出——不能当主升浪买
    elif rs20 > -3.0:
        stage = "观望期"           # 中性区间
    else:
        stage = "退潮期"           # 跑输但资金未流出，可能是洗盘，标退潮观察

    return {
        "code": code, "name": name,
        "stage": stage,
        "totalScore": round(total_score, 1),
        "scores": {"rs": round(rs_score, 1), "flow": round(flow_score + breadth_adj, 1), "crowd": round(crowd_score, 1)},
        "rs5": round(rs5, 2), "rs20": round(rs20, 2), "rs60": round(rs60, 2),
        "absD5": round(d5, 2), "absD20": round(d20, 2), "absD60": round(d60, 2),
        "mainFlow5Wan": round(f5, 0), "mainFlow20Wan": round(f20, 0),
        "mainFlow5Rel": round(f5_rel, 3), "mainFlow20Rel": round(f20_rel, 3),
        "upRatio": round(up_ratio, 1), "upCount": up, "totalCount": total,
        "turnoverWan": round(turnover, 0), "circMvYi": round(ltsz, 1), "hsl": round(hsl, 2),
        "overheat": overheat, "hotness": round(hot, 2),
        "dataTier": {
            "price_flow_breadth": "真实公开数据（腾讯接口原值）",
            "crowding": "量价代理（无 60 日历史序列，用换手+5日涨幅+主力净流入拐头估算）",
        },
        "benchUsed": "中证800" if bench else "无（基准拉取失败，RS 退化为绝对涨幅）",
    }


def build_rotation(board_type="hy", max_n=None, top=None):
    boards = fetch_boards(board_type, max_n or (31 if board_type != "gn" else 808))
    if not boards:
        return {"ok": False, "error": "板块列表为空", "type": board_type}
    bench = fetch_bench_returns()
    # 全市场成交额 = 各板块成交额之和（近似），用于占比
    total_turnover = sum(_f(b.get("turnover")) for b in boards) or 1.0
    scored, dropped_pseudo, dropped_micro = [], 0, 0
    for b in boards:
        name = str(b.get("name") or "")
        # 概念板块：剔除风格/持股/指数类伪概念（黑名单 + 正则）
        if board_type == "gn" and _is_pseudo_concept(name):
            dropped_pseudo += 1
            continue
        s = score_board(b, bench, 0)
        if s:
            s["amtSharePct"] = round(s["turnoverWan"] / total_turnover * 100, 2)
            # 概念板块：剔除微概念（流动性太差，机构进不去，波段无意义）
            if board_type == "gn" and s["amtSharePct"] < MIN_AMT_SHARE_PCT:
                dropped_micro += 1
                continue
            scored.append(s)
    scored.sort(key=lambda x: -x["totalScore"])
    if top:
        scored = scored[:top]
    stage_count = {}
    for s in scored:
        stage_count[s["stage"]] = stage_count.get(s["stage"], 0) + 1
    return {
        "ok": True,
        "type": board_type,
        "generatedAt": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
        "generatedAtTz": "Asia/Shanghai (UTC+8)",
        "nBoards": len(scored),
        "nRaw": len(boards),
        "droppedPseudoConcepts": dropped_pseudo,
        "droppedMicroConcepts": dropped_micro,
        "bench": {"code": BENCH_CODE, "name": "中证800", "returns": bench and {k: round(v, 2) for k, v in bench.items()}},
        "stageCount": stage_count,
        "dataTierNote": "涨跌幅/主力净流入/成交额/流通市值/涨跌家数为真实公开数据；拥挤度为量价代理（非 60 日分位）。",
        "boards": scored,
    }


def main():
    ap = argparse.ArgumentParser(description="行业/题材轮动引擎")
    ap.add_argument("--type", default="hy", choices=["hy", "gn", "all"], help="hy=行业 gn=概念 all=两者")
    ap.add_argument("--top", type=int, default=15, help="每类取前 N")
    ap.add_argument("--out", default=None, help="输出 JSON 路径")
    args = ap.parse_args()

    types = ["hy", "gn"] if args.type == "all" else [args.type]
    result = {"ok": True, "generatedAt": now_beijing().strftime("%Y-%m-%d %H:%M:%S")}
    for t in types:
        r = build_rotation(t, top=args.top)
        result[t] = r
        if not r.get("ok"):
            print("[%s] 失败: %s" % (t, r.get("error")), file=sys.stderr)
            continue
        print("=== %s 板块轮动 Top %d（基准 %s）===" % ("行业" if t == "hy" else "概念", len(r["boards"]), r["bench"]["name"]))
        print("  阶段分布: %s" % r["stageCount"])
        print("  %-10s %-6s %6s %7s %7s %6s %6s %5s" % ("名称", "阶段", "总分", "RS20", "主力20", "涨占比", "换手%", "占比%"))
        for s in r["boards"]:
            print("  %-10s %-6s %6.1f %7.2f %7.0f %5.0f%% %6.2f %5.2f" % (
                s["name"][:10], s["stage"], s["totalScore"], s["rs20"], s["mainFlow20Rel"], s["upRatio"], s["hsl"], s["amtSharePct"]))
    if args.out:
        p = pathlib.Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写入 %s" % p, file=sys.stderr)


if __name__ == "__main__":
    main()
