#!/usr/bin/env python3
"""EdsonStock × A股超短游资实盘信号与每日自动选股 Docker 服务端 (live_signal_server.py)。

功能（100% 零第三方依赖，Python 3.8+ 标准库）：
  1) 托管 `/web/index.html` 超短游资实盘工作台网页（访问 http://<NAS_IP>:8899/ 即可直接打开）；
  2) 内置后台自动选股调度器（Scheduler Thread）：
     - 每个交易日 09:25:05 自动执行 `generate_top10_picks(mode="auction")`（9:25 集合竞价弱转强动态重排 Top 10）；
     - 每个交易日 15:05:00 自动执行 `generate_top10_picks(mode="post")`（收盘涨跌停池初选 Top 10）；
     - 每个交易日 17:35:00 自动执行 `generate_top10_picks(mode="post")`（龙虎榜游资/机构席位披露后终选 Top 10）；
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
from http.server import BaseHTTPRequestHandler, HTTPServer
from realtime_and_short import fetch_em_pool, http_get, normalize_code
from query import load as edson_load
from auto_pick_top10 import generate_top10_picks

WEBHOOK_URL = os.environ.get("EDSON_WEBHOOK_URL", "")
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
WEB_INDEX = BASE_DIR / "web" / "index.html"
DAILY_PICKS_FILE = BASE_DIR / "web" / "daily_picks.json"


def run_auto_picker(mode="post"):
    try:
        res = generate_top10_picks(None, mode=mode)
        DAILY_PICKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        DAILY_PICKS_FILE.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[AutoPicker] 已自动更新 Top 10 选股池 (%s | mode=%s | 共 %d 只)" %
              (res.get("tradeDate"), mode, len(res.get("picks") or [])))
        return res
    except Exception as e:
        print("[AutoPicker Error]", e)
        return None


def background_scheduler_loop():
    """后台定时选股守护线程：交易日 09:25、15:05、17:35 自动刷新 daily_picks.json。"""
    triggered = set()
    # 启动时如果还没有 daily_picks.json，立即生成一次
    if not DAILY_PICKS_FILE.exists():
        run_auto_picker("post")
    while True:
        now = dt.datetime.now()
        if now.weekday() < 5:  # 周一至周五
            hm = now.strftime("%H:%M")
            day_key = now.strftime("%Y%m%d") + "_" + hm
            if hm == "09:25" and day_key not in triggered:
                triggered.add(day_key)
                run_auto_picker("auction")
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

    # 启动后台自动选股定时线程 (09:25 / 15:05 / 17:35 自动刷新 Top 10)
    t = threading.Thread(target=background_scheduler_loop, daemon=True)
    t.start()

    srv = HTTPServer((args.host, args.port), SignalHandler)
    print("=== EdsonStock A股超短实盘信号与每日自动选股网关已启动: http://%s:%d ===" % (args.host, args.port))
    print("  - 超短信号前端工作台 : http://%s:%d/" % (args.host, args.port))
    print("  - 每日自动选股 Top10 : http://%s:%d/api/daily_picks (支持 ?refresh=1&mode=post|auction)" % (args.host, args.port))
    print("  - 实时五档与放量接口 : http://%s:%d/api/quotes?codes=603324,002771,002164" % (args.host, args.port))
    srv.serve_forever()


if __name__ == "__main__":
    main()
