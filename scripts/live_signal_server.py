#!/usr/bin/env python3
"""EdsonStock × A股超短游资实盘信号与每日自动选股 Docker 服务端 (live_signal_server.py)。

功能（100% 零第三方依赖，Python 3.8+ 标准库）：
  1) 托管 `/web/index.html` 超短游资实盘工作台网页（访问 http://<NAS_IP>:8899/ 即可直接打开）；
  2) 内置后台自动选股调度器（Scheduler Thread，统一北京时间 UTC+8）：
     - 每个交易日 09:25 自动执行 `generate_top10_picks(mode="auction")`（9:25 集合竞价真弱转强重排 Top 10）；
     - 每个交易日 09:35 / 09:45 自动执行 `run_intraday_replacement()`：剔除开盘跌幅 <= -3% 的弱势股，
       补入当日 09:30~09:40 最先封板且封流比 >2% 的主线新卡位股；
     - 每个交易日 15:05 自动执行 `generate_top10_picks(mode="post")`（收盘涨跌停池初选 Top 10）；
     - 每个交易日 17:35 自动执行 `generate_top10_picks(mode="post")`（龙虎榜游资/机构席位披露后终选 Top 10）；
     - 选股结果自动持久化写入 `/web/daily_picks.json`，前端打开页面即自动同步最新 10 只标的！
  3) 提供 `/api/daily_picks?refresh=1&mode=post|auction` 接口：支持网页端一键手动触发重新选股；
  4) 提供 `/api/zt_pool` 与 `/api/quotes` 接口：实时拉取东财三池与腾讯五档盘口；
  5) 提供 `/api/webhook_push` 接口：将信号真实 POST 推送至飞书、钉钉、企业微信或 iOS Bark 终端。

启动方式：
  python3 scripts/live_signal_server.py --host 0.0.0.0 --port 8899 --webhook "https://你的飞书或钉钉Webhook地址(可选)"
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from realtime_and_short import fetch_em_pool, http_get, normalize_code
from query import load as edson_load
from auto_pick_top10 import generate_top10_picks, fetch_l1_map

WEBHOOK_URL = os.environ.get("EDSON_WEBHOOK_URL", "")
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
WEB_INDEX = BASE_DIR / "web" / "index.html"
DAILY_PICKS_FILE = BASE_DIR / "web" / "daily_picks.json"
MIN_VALID_PICKS = 3  # 与 CI 校验一致：少于此数视为无效结果，不覆盖已有文件

# Webhook 冷却：同一 (事件类型, 代码) 在冷却窗口内只推一次，避免刷屏
_WEBHOOK_COOLDOWN_SEC = 1800
_webhook_last_sent = {}
_webhook_lock = threading.Lock()


def now_beijing():
    """统一北京时间（UTC+8），不依赖运行环境 TZ。"""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=8)


def send_webhook(event_type, code, title, lines):
    """主动推送 Webhook（飞书/钉钉/企业微信/Bark 通用 JSON），带冷却去重。未配置 URL 时静默跳过。"""
    if not WEBHOOK_URL:
        return False
    key = (event_type, str(code or ""))
    now = time.time()
    with _webhook_lock:
        last = _webhook_last_sent.get(key, 0)
        if now - last < _WEBHOOK_COOLDOWN_SEC:
            return False
        _webhook_last_sent[key] = now
    stamp = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    body = "%s\n%s\n%s" % (title, "\n".join(lines), stamp)
    payload = {
        "msg_type": "text",
        "text": {"content": body},
        # 兼容三种常见自定义机器人格式
        "feishu": {"msg_type": "text", "content": {"text": body}},
        "dingtalk": {"msgtype": "text", "text": {"content": body}},
        "wecom": {"msgtype": "text", "text": {"content": body}},
        "meta": {"event": event_type, "code": code, "time": stamp},
    }
    try:
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
        print("[Webhook] 已推送 %s %s" % (event_type, code))
        return True
    except Exception as e:
        print("[Webhook Error]", e)
        return False


def run_auto_picker(mode="post"):
    """跑选股并落盘。结果无效（< MIN_VALID_PICKS 只或 ok=False）时保留原文件，不覆盖。"""
    try:
        res = generate_top10_picks(None, mode=mode)
        picks = (res or {}).get("picks") or []
        if not (res or {}).get("ok") or len(picks) < MIN_VALID_PICKS:
            print("[AutoPicker] 结果无效 (%d 只)，保留原 daily_picks.json 不覆盖" % len(picks))
            return None
        DAILY_PICKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        DAILY_PICKS_FILE.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[AutoPicker] 已自动更新 Top 10 选股池 (%s | mode=%s | 共 %d 只)" %
              (res.get("tradeDate"), mode, len(picks)))
        if mode in ("post", "auction"):
            top = ", ".join("%s(%s)" % (p.get("name"), p.get("code")) for p in picks[:5])
            send_webhook("picks_%s" % mode, "ALL", "【EdsonStock】%s 选股完成 (%d 只)" % (
                "盘后" if mode == "post" else "竞价重排", len(picks)), ["前5: " + top])
        return res
    except Exception as e:
        print("[AutoPicker Error]", e)
        return None


def run_intraday_replacement(check_time):
    """09:35 / 09:45 早盘卡位动态替补。

    剔除 Top 10 中开盘跌幅 <= -3% 的弱势股，补入当日 09:30~09:40 最先封板
    且封流比(封单/流通市值) > 2% 的主线新卡位股；无有效候选时保持原文件不变。
    """
    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    def _hhmmss(raw):
        s = str(raw or "").strip().zfill(6)
        return "%s:%s:%s" % (s[:2], s[2:4], s[4:6]) if len(s) == 6 and s.isdigit() else "--:--:--"

    if not DAILY_PICKS_FILE.exists():
        return None
    try:
        data = json.loads(DAILY_PICKS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print("[Intraday %s] 读取 daily_picks.json 失败: %s" % (check_time, e))
        return None

    picks = data.get("picks") or []
    if not picks:
        return None

    quotes = fetch_tencent_quotes(",".join(str(p.get("code") or "") for p in picks if p.get("code")))
    qmap = {q.get("code"): q for q in quotes}
    weak = [p for p in picks if _f((qmap.get(p.get("code")) or {}).get("openGap")) <= -3.0]
    if not weak:
        print("[Intraday %s] 无开盘跌幅 <= -3%% 的弱势股，保持原 Top 10。" % check_time)
        return data

    weak_codes = {str(p.get("code") or "") for p in weak}
    kept = [p for p in picks if str(p.get("code") or "") not in weak_codes]
    existing = {str(p.get("code") or "") for p in kept}
    removed = [{"code": p.get("code"), "name": p.get("name"), "openGap": _f((qmap.get(p.get("code")) or {}).get("openGap"))} for p in weak]

    now_bj = now_beijing()
    date_str = now_bj.strftime("%Y%m%d")
    zt_pool = (fetch_em_pool("getTopicZTPool", date_str) or {}).get("pool") or []
    top_sectors = [s.get("name") for s in (data.get("marketSummary") or {}).get("topSectors") or []][:3]

    cards = []
    for x in zt_pool:
        fbt = str(x.get("fbt") or "").strip().zfill(6)
        if not ("093000" <= fbt <= "094000"):
            continue
        code = str(x.get("c") or "")
        if not code or code in existing or code in weak_codes:
            continue
        sector = x.get("hybk") or "其他"
        if top_sectors and sector not in top_sectors:
            continue
        cards.append((code, x, _hhmmss(fbt), sector))

    qmap_l1 = fetch_l1_map([c[0] for c in cards]) if cards else {}
    scored = []
    for code, x, fbt_str, sector in cards:
        circ = _f((qmap_l1.get(code) or {}).get("circMv"))
        if circ <= 0:
            continue
        seal_yi = _f(x.get("fund")) / 1e8
        flow = round(seal_yi / max(0.01, circ) * 100.0, 2)
        if flow > 2.0:
            scored.append((flow, code, x, fbt_str, sector, seal_yi, circ))
    scored.sort(key=lambda t: -t[0])

    if not scored:
        print("[Intraday %s] 检出 %d 只弱势股，但无符合 09:30~09:40 首封且封流比>2%% 的主线卡位股，保持原 Top 10。" %
              (check_time, len(weak)))
        return data

    added = []
    for flow, code, x, fbt_str, sector, seal_yi, circ in scored:
        if len(kept) >= 10:
            break
        try:
            prefix = normalize_code(code)[1]
        except Exception:
            prefix = ""
        amount_yi = round(_f(x.get("amount")) / 1e8, 2)
        kept.append({
            "code": code, "prefix": prefix, "name": str(x.get("n") or ""), "sector": sector,
            "boards": 1, "boardLabel": "%s早盘卡位" % check_time,
            "signalType": "EARLY_CARD",
            "signalTag": "【%s卡位替补】%s首封·封流比%.2f%%" % (check_time, fbt_str, flow),
            "price": 0, "preClose": 0, "open": 0, "openGap": 0, "chg": 0,
            "volHand": 0, "outerVol": 0, "innerVol": 0, "activeDiff": 0, "deltaHands": 0,
            "firstSeal": fbt_str, "brokenCount": 0,
            "sealYi": round(seal_yi, 2), "amountYi": amount_yi,
            "sealRatio": round(seal_yi / max(0.01, amount_yi) * 100.0, 1),
            "sealFlowRatio": flow, "sealMvRatio": flow,
            "circMv": circ, "turnover": _f(x.get("hs")), "volRatio": 1.0,
            "limitUp": 0, "limitDown": 0, "quoteTime": "",
            "bids": [[0, 0]] * 5, "asks": [[0, 0]] * 5,
            "lhbSummary": "09:30~09:40 早盘最先封板且封流比 >2% 的主线卡位股",
            "lhbSeats": [{"side": "卡位", "tag": "主线", "dept": "%s 板块 · 首封 %s" % (sector, fbt_str),
                          "net": "封流比 %.2f%%" % flow}],
            "scoreParts": {},
            "backtest": {"wtsRet": "早盘卡位替补", "wtsMdd": "首封 %s" % fbt_str,
                         "wtsSharpe": "1板", "wtsWin": "%s 主线" % sector,
                         "mrRet": "封单 %.2f亿" % seal_yi, "boRet": "封流比 %.2f%%" % flow,
                         "note": "09:35/09:45 动态替补，剔除开盘跌破 -3% 的弱势股后补入"},
        })
        existing.add(code)
        added.append({"code": code, "name": str(x.get("n") or ""), "firstSeal": fbt_str, "sealFlowRatio": flow})

    data["picks"] = kept[:10]
    data["generatedAt"] = now_bj.strftime("%Y-%m-%d %H:%M:%S")
    data["generatedAtTz"] = "Asia/Shanghai (UTC+8)"
    data["mode"] = "intraday_%s" % check_time.replace(":", "")
    data["intradayReplacement"] = {"checkTime": check_time, "removed": removed, "added": added}
    DAILY_PICKS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[Intraday %s] 剔除 %d 只弱势股，补入 %d 只早盘卡位股，当前 %d 只。" %
          (check_time, len(removed), len(added), len(data["picks"])))
    # 替补发生时推送 Webhook
    lines = []
    if removed:
        lines.append("剔除: " + ", ".join("%s(%.2f%%)" % (r.get("name"), r.get("openGap") or 0) for r in removed))
    if added:
        lines.append("补入: " + ", ".join("%s(%s 首封%s 封流比%.2f%%)" % (
            a.get("name"), a.get("code"), a.get("firstSeal"), a.get("sealFlowRatio") or 0) for a in added))
    send_webhook("intraday_%s" % check_time, "ALL", "【EdsonStock】%s 早盘卡位替补" % check_time, lines)
    return data


def run_auction_watch():
    """09:20~09:25 竞价末端监控：对 Top 10 拉取竞价匹配价，识别「竞价抢筹/砸盘」并推送。

    竞价阶段腾讯 openPx(p[5]) 可能为 0，改用 p[3]（当前匹配价）对比昨收计算竞价涨跌幅。
    """
    if not DAILY_PICKS_FILE.exists():
        return None
    try:
        data = json.loads(DAILY_PICKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    picks = data.get("picks") or []
    if not picks:
        return None

    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    quotes = fetch_tencent_quotes(",".join(str(p.get("code") or "") for p in picks if p.get("code")))
    alerts = []
    for q in quotes:
        px, pre = _f(q.get("price")), _f(q.get("preClose"))
        if px <= 0 or pre <= 0:
            continue
        gap = (px / pre - 1.0) * 100.0
        name = q.get("name") or q.get("code")
        if gap >= 7.0:
            alerts.append((name, q.get("code"), gap, "竞价抢筹/一字风险"))
        elif gap <= -3.0:
            alerts.append((name, q.get("code"), gap, "竞价砸盘/核按钮风险"))
    for name, code, gap, tag in alerts:
        send_webhook("auction_" + ("up" if gap > 0 else "down"), code,
                     "【EdsonStock】竞价异动 %s" % tag,
                     ["%s(%s) 竞价 %+.2f%%" % (name, code, gap)])
    if alerts:
        print("[Auction] 竞价异动 %d 条" % len(alerts))
    return alerts


def background_scheduler_loop():
    """后台定时守护线程：交易日 09:20~09:25 竞价监控 / 09:25 / 09:35 / 09:45 / 15:05 / 17:35 自动刷新 daily_picks.json。"""
    triggered = set()
    # 启动时如果还没有 daily_picks.json，立即生成一次
    if not DAILY_PICKS_FILE.exists():
        run_auto_picker("post")
    while True:
        now = now_beijing()
        if now.weekday() < 5:  # 周一至周五
            hm = now.strftime("%H:%M")
            day_key = now.strftime("%Y%m%d") + "_" + hm
            # 09:20~09:24 竞价末端：每分钟一次监控（09:25 由 auction 重排接管）
            if "09:20" <= hm <= "09:24" and day_key not in triggered:
                triggered.add(day_key)
                run_auction_watch()
            if hm == "09:25" and day_key not in triggered:
                triggered.add(day_key)
                run_auto_picker("auction")
            elif hm in ("09:35", "09:45") and day_key not in triggered:
                triggered.add(day_key)
                run_intraday_replacement(hm)
            elif hm in ("15:05", "17:35") and day_key not in triggered:
                triggered.add(day_key)
                run_auto_picker("post")
        time.sleep(15)


def fetch_tencent_quotes(codes_csv):
    raw_list = [c.strip() for c in codes_csv.split(",") if c.strip()]
    if not raw_list:
        return []
    norm_list = []
    for c in raw_list:
        try:
            norm_list.append(normalize_code(c)[1])
        except Exception:
            pass
    if not norm_list:
        return []
    url = "https://qt.gtimg.cn/q=" + ",".join(norm_list)
    text = http_get(url, encoding="gbk")
    results = []
    for line in text.strip().splitlines():
        if "~" not in line:
            continue
        p = line.split("~")
        if len(p) < 50:
            continue
        bids = [[float(p[9 + i * 2] or 0), int(float(p[10 + i * 2] or 0))] for i in range(5)]
        asks = [[float(p[19 + i * 2] or 0), int(float(p[20 + i * 2] or 0))] for i in range(5)]
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

        is_zt = limit_up > 0 and abs(now_px - limit_up) <= 0.015
        seal_yi = (bids[0][0] * bids[0][1] * 100.0) / 1e8 if is_zt else 0.0
        amt_yi = amt_wan / 10000.0
        seal_ratio = round((seal_yi / max(0.01, amt_yi)) * 100.0, 2) if is_zt else 0.0

        results.append({
            "name": p[1],
            "code": p[2],
            "price": now_px,
            "preClose": pre_c,
            "open": open_px,
            "openGap": round((open_px / pre_c - 1.0) * 100.0, 2) if pre_c > 0 else 0.0,
            "chg": chg_pct,
            "volHand": vol_hand,
            "outerVol": outer_vol,
            "innerVol": inner_vol,
            "activeDiff": outer_vol - inner_vol,
            "amountYi": round(amt_yi, 2),
            "turnover": turnover,
            "circMv": circ_mv,
            "limitUp": limit_up,
            "limitDown": limit_down,
            "volRatio": vol_ratio,
            "sealYi": round(seal_yi, 2),
            "sealRatio": seal_ratio,
            "bids": bids,
            "asks": asks,
            "time": p[30]
        })
    return results


class SignalHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_bytes, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_bytes)))
        self.end_headers()
        self.wfile.write(html_bytes)

    def do_OPTIONS(self):
        self._send_json({"ok": True})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html"):
            if WEB_INDEX.exists():
                self._send_html(WEB_INDEX.read_bytes())
                return
            self._send_json({"ok": True, "service": "EdsonStock Live Ultra-Short Signal Server"})
        elif parsed.path in ("/daily_picks.json", "/api/daily_picks"):
            refresh = qs.get("refresh", ["0"])[0] == "1"
            mode = qs.get("mode", ["post"])[0]
            if refresh or not DAILY_PICKS_FILE.exists():
                res = run_auto_picker(mode)
                if res:
                    self._send_json(res)
                    return
            if DAILY_PICKS_FILE.exists():
                try:
                    self._send_json(json.loads(DAILY_PICKS_FILE.read_text(encoding="utf-8")))
                    return
                except Exception:
                    pass
            self._send_json({"ok": False, "error": "daily_picks.json 尚未生成"}, status=404)
        elif parsed.path == "/api/quotes":
            codes = qs.get("codes", ["002771,603324,002579,000823,002642,600360"])[0]
            self._send_json({"ok": True, "quotes": fetch_tencent_quotes(codes)})
        elif parsed.path == "/api/zt_pool":
            d_str = qs.get("date", [dt.datetime.now().strftime("%Y%m%d")])[0]
            zt = fetch_em_pool("getTopicZTPool", d_str)
            zb = fetch_em_pool("getTopicZBPool", d_str)
            dt_p = fetch_em_pool("getTopicDTPool", d_str)
            self._send_json({
                "ok": True,
                "date": zt.get("qdate") or d_str,
                "ztPool": zt.get("pool") or [],
                "zbPool": zb.get("pool") or [],
                "dtPool": dt_p.get("pool") or []
            })
        elif parsed.path == "/api/edson":
            what = qs.get("what", ["erp"])[0]
            try:
                data, src = edson_load(what, True)
                self._send_json({"ok": True, "what": what, "source": src, "data": data})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
        elif parsed.path == "/health":
            self._send_json({"ok": True, "status": "healthy"})
        else:
            self._send_json({"ok": False, "error": "Not Found"}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8", "ignore")
        try:
            payload = json.loads(raw_body)
        except Exception:
            payload = {}
        target_url = payload.get("webhookUrl") or WEBHOOK_URL
        card_body = payload.get("payload") or payload
        if not target_url:
            self._send_json({"ok": False, "error": "未配置 Webhook URL"}, status=400)
            return
        try:
            req = urllib.request.Request(
                target_url,
                data=json.dumps(card_body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                resp_text = r.read().decode("utf-8", "ignore")
            self._send_json({"ok": True, "response": resp_text})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, status=500)


def main():
    global WEBHOOK_URL
    ap = argparse.ArgumentParser(description="EdsonStock A股超短实盘信号与每日自动选股本地服务")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8899, help="监听端口 (默认 8899)")
    ap.add_argument("--webhook", default="", help="默认飞书/钉钉/企业微信/Bark Webhook URL")
    args = ap.parse_args()
    if args.webhook:
        WEBHOOK_URL = args.webhook

    # 启动后台自动选股定时线程 (09:25 竞价 / 09:35+09:45 卡位替补 / 15:05+17:35 盘后)
    t = threading.Thread(target=background_scheduler_loop, daemon=True)
    t.start()

    srv = ThreadingHTTPServer((args.host, args.port), SignalHandler)
    print("=== EdsonStock A股超短实盘信号与每日自动选股网关已启动: http://%s:%d ===" % (args.host, args.port))
    print("  - 调度时点(北京时间): 09:25 竞价重排 | 09:35/09:45 早盘卡位替补 | 15:05/17:35 盘后选股")
    print("  - 超短信号前端工作台 : http://%s:%d/" % (args.host, args.port))
    print("  - 每日自动选股 Top10 : http://%s:%d/api/daily_picks (支持 ?refresh=1&mode=post|auction)" % (args.host, args.port))
    print("  - 实时五档与放量接口 : http://%s:%d/api/quotes?codes=603324,002771,002164" % (args.host, args.port))
    srv.serve_forever()


if __name__ == "__main__":
    main()
