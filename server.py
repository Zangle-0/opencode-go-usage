"""OpenCode Go 用量看板 - 本地代理后端 (仅标准库, 无需 pip 安装).

运行:  python server.py [--port 8765]
打开:  http://localhost:8765

前端把 Service Account Key (oc_sk_...) 放在请求头 X-Api-Key 发给本代理,
代理再以 Authorization: Bearer 转调官方 Console, 避免浏览器 CORS 和 Key 裸奔到第三方.
也支持服务端预置: set CONSOLE_API_KEY=oc_sk_... (或写 .env 文件).
"""
import csv
import datetime
import io
import json
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONSOLE_URL = os.environ.get("CONSOLE_URL", "https://opencode.ai/console").rstrip("/")
ZEN_URL = os.environ.get("ZEN_URL", "https://opencode.ai/zen").rstrip("/")
DEFAULT_PORT = int(os.environ.get("PORT", "8765"))

# .env 简易加载 (key=value, 忽略 # 注释)
_env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    CONSOLE_URL = os.environ.get("CONSOLE_URL", CONSOLE_URL).rstrip("/")


def load_limits():
    with open(os.path.join(BASE_DIR, "GO_LIMITS.json"), encoding="utf-8") as f:
        return json.load(f)


def norm_model(s):
    return (s or "").strip().lower()


def _api_get(api_key, path, params, accept, base=None):
    """调 Console/Zen API, 返回文本. 失败时抛出带状态码的异常."""
    url = (base or CONSOLE_URL) + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + api_key,
        "Accept": accept,
        "User-Agent": "go-usage-viewer/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"官方接口 {e.code}: {body}")


def fetch_v2_csv(api_key, range_, user_id=None):
    """v2 按天汇总导出. range 仅支持 7d/30d/90d, 含今日UTC.
    v1 已下线(组织迁v2后401/403), 不再使用."""
    params = {"range": range_}
    if user_id:
        params["user_id"] = user_id
    return _api_get(api_key, "/api/v2/usage/export", params, "text/csv")


def fetch_go_limits(api_key):
    """Console Go 页同款官方限额. 返回 {model_id: {display, monthly_limit}}."""
    data = json.loads(_api_get(api_key, "/api/go/limits", {}, "application/json"))
    out = {}
    for m in data.get("models", []):
        mid = norm_model(m.get("id"))
        if not mid:
            continue
        go = (m.get("limits") or {}).get("go") or {}
        out[mid] = {"display": m.get("name") or m.get("id"),
                    "monthly_limit": go.get("usage")}
    return out


def fetch_zen_usage(api_key):
    """官方总体用量: {rolling, weekly, monthly: {status, percent, resetsAt}}.
    无分模型维度, 失败时由调用方吞掉(表格照常工作)."""
    data = json.loads(_api_get(api_key, "/go/v1/usage", {}, "application/json", ZEN_URL))
    return data.get("usage")


def _f(row, *keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except ValueError:
                return 0.0
    return 0.0


def _new_agg():
    return {"spent_usd": 0.0, "billable_usd": 0.0, "records": 0, "requests": 0,
            "by_billing": {}, "by_provider": {},
            "input_tokens": 0.0, "output_tokens": 0.0,
            "cache_read_tokens": 0.0, "cache_write_tokens": 0.0,
            "today_cost": 0.0}


def aggregate(csv_text, today=None):
    """聚合用量 CSV. 返回 (go_models, extras).

    v2 schema 按UTC天汇总: day,user_type,...,provider,model,requests,
    input/output/cache_*_tokens,cost_micro_cents. v2 的 cost 已是 Go 定价,
    直接信任; 无 billing_source 列, 用 provider=='opencode-go' 圈定订阅用量.
    v1 schema (无 day 列) 保留兼容: 按 billing 排除 byok/free.
    today: 'YYYY-MM-DD' (UTC), 用于今日切片.
    """
    out, extras = {}, {}
    reader = csv.DictReader(io.StringIO(csv_text))
    v2 = bool(reader.fieldnames and "day" in reader.fieldnames)
    for row in reader:
        mid = norm_model(row.get("model"))
        if not mid:
            continue  # web-search 等非模型行跳过
        usd = _f(row, "cost_micro_cents") / 100_000_000
        billing = (row.get("billing_source") or "").strip() or "unknown"
        provider = (row.get("provider") or "").strip()
        tok_in = _f(row, "input_tokens")
        tok_out = _f(row, "output_tokens", "reasoning_tokens")
        tok_cr = _f(row, "cache_read_tokens")
        tok_cw = _f(row, "cache_write_5m_tokens") + _f(row, "cache_write_1h_tokens")
        go_row = (provider == "opencode-go") if v2 else (billing not in ("byok", "free"))
        if go_row:
            key, target = mid, out
        else:
            key, target = provider + " / " + mid if provider else mid, extras
        entry = target.setdefault(key, _new_agg())
        entry["spent_usd"] += usd
        entry["billable_usd"] += usd
        entry["records"] += 1
        entry["requests"] += int(_f(row, "requests"))
        entry["by_billing"][billing] = entry["by_billing"].get(billing, 0) + usd
        entry["by_provider"][provider] = entry["by_provider"].get(provider, 0) + 1
        entry["input_tokens"] += tok_in
        entry["output_tokens"] += tok_out
        entry["cache_read_tokens"] += tok_cr
        entry["cache_write_tokens"] += tok_cw
        if v2 and today and (row.get("day") or "")[:10] == today:
            entry["today_cost"] += usd
    return out, extras


def go_priced(agg, price):
    """按 Go 单价表 ($/1M tokens) 估算订阅额度消耗."""
    if not price:
        return 0.0
    inp = agg.get("input_tokens", 0) * (price.get("input") or 0)
    outp = agg.get("output_tokens", 0) * (price.get("output") or 0)
    cr = agg.get("cache_read_tokens", 0) * (price.get("cached_read") or 0)
    cw = agg.get("cache_write_tokens", 0) * (price.get("cached_write") or 0)
    return (inp + outp + cr + cw) / 1_000_000


class Handler(BaseHTTPRequestHandler):
    server_version = "GoUsageViewer/1.0"

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-Api-Key, X-Access-Pin, Content-Type")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, "")

    def _pin_ok(self):
        expected = os.environ.get("VIEWER_PIN", "")
        if not expected:
            return True  # 未设 PIN (本地直跑) 则不校验
        return self.headers.get("X-Access-Pin") == expected

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/api/") and not self._pin_ok():
            self._send(403, json.dumps({"error": "需要访问 PIN: 请在页面填写 NAS 上设置的 VIEWER_PIN"}))
            return

        if path in ("/", "/index.html"):
            with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html")
            return
        if path == "/api/limits":
            with open(os.path.join(BASE_DIR, "GO_LIMITS.json"), "rb") as f:
                self._send(200, f.read())
            return
        if path == "/api/models":
            # 透传官方 Go 模型列表, 无需鉴权, 失败不致命
            try:
                req = urllib.request.Request("https://opencode.ai/zen/go/v1/models",
                                             headers={"User-Agent": "go-usage-viewer/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self._send(200, resp.read())
            except Exception as e:  # noqa: BLE001
                self._send(502, json.dumps({"error": str(e)}))
            return
        if path == "/api/usage":
            api_key = self.headers.get("X-Api-Key") or os.environ.get("CONSOLE_API_KEY", "")
            if not api_key:
                self._send(401, json.dumps({"error": "缺少 Key: 请在页面填写 Service Account Key (oc_sk_...), 或在服务端设置 CONSOLE_API_KEY"}))
                return
            user_id = qs.get("user_id", [None])[0]
            try:
                today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
                per30, ex30 = aggregate(fetch_v2_csv(api_key, "30d", user_id), today)
                per7, _ex7 = aggregate(fetch_v2_csv(api_key, "7d", user_id), today)
                try:
                    zen = fetch_zen_usage(api_key)
                except Exception:
                    zen = None  # 总体条拿不到不影响表格
                pricebook = {norm_model(m["id"]): m.get("price") for m in load_limits()["models"]}
                try:
                    live = fetch_go_limits(api_key)
                    models = [{"id": mid, "display": info["display"],
                               "monthly_limit_usd": info["monthly_limit"],
                               "price": pricebook.get(mid), "note": "live"}
                              for mid, info in live.items()]
                    limits_source = "live go/limits"
                except Exception:
                    models = load_limits()["models"]
                    limits_source = "local fallback"
                prices = {norm_model(m["id"]): m.get("price") for m in models}
                rows = []
                for m in models:
                    mid = norm_model(m["id"])
                    a30 = per30.get(mid, _new_agg())
                    a7 = per7.get(mid, _new_agg())
                    # v2 cost 已是 Go 定价; token×单价仅作兜底, 取较大者.
                    # 今日切片取 30d 行里的当日 UTC 累计 (7d 同天, 取 30d 即可).
                    s30 = max(a30.get("billable_usd", 0), go_priced(a30, prices.get(mid)))
                    s7 = max(a7.get("billable_usd", 0), go_priced(a7, prices.get(mid)))
                    sday = a30.get("today_cost", 0)
                    raw_limit = m.get("monthly_limit_usd")
                    unlimited = raw_limit is None
                    limit = None if unlimited else float(raw_limit)
                    rows.append({
                        "id": m["id"], "display": m.get("display", m["id"]),
                        "unlimited": unlimited,
                        "monthly_limit": limit,
                        "weekly_limit": None if unlimited else round(limit * 0.5, 4),
                        "five_h_limit": None if unlimited else round(limit * 0.2, 4),
                        "spent_30d": round(s30, 4),
                        "spent_7d": round(s7, 4),
                        "spent_today": round(sday, 4),
                        "remaining_month": None if unlimited else round(limit - s30, 4),
                        "remaining_pct": None if unlimited else round(max(0, (limit - s30) / limit * 100), 1),
                        "note": m.get("note", ""),
                        "tok_in_30d": int(a30.get("input_tokens", 0)),
                        "tok_out_30d": int(a30.get("output_tokens", 0)),
                        "tok_cache_30d": int(a30.get("cache_read_tokens", 0) + a30.get("cache_write_tokens", 0)),
                        "tok_total_30d": int(a30.get("input_tokens", 0) + a30.get("output_tokens", 0) + a30.get("cache_read_tokens", 0) + a30.get("cache_write_tokens", 0)),
                        "detail_30d": per30.get(mid),
                    })
                # 官方限额表之外的模型 (有花费但无 suerte限额): 追加展示, 方便核对 provider 映射
                known = {norm_model(m["id"]) for m in models}
                extras = []
                _all = dict(per30)
                _all.update(ex30)
                for mid, agg in _all.items():
                    if mid not in known and agg.get("records", 0) > 0:
                        extras.append({"id": mid, "billable_30d": round(agg["billable_usd"], 4),
                                       "records": agg["records"], "by_billing": agg["by_billing"],
                                       "by_provider": agg.get("by_provider", {})})
                self._send(200, json.dumps({"rows": rows, "unknown_models": extras,
                                            "zen": zen,
                                            "limits_source": limits_source, "warn": "v2按UTC天汇总(含今日),数据可滞后数小时;7d≈周,30d≈月,今日≈5h窗口参考"}))
            except RuntimeError as e:
                self._send(502, json.dumps({"error": str(e)}))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *args):
        pass  # 保持控制台干净


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", str(DEFAULT_PORT))))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    args = ap.parse_args()
    print(f"Go 用量看板运行中: http://{args.host}:{args.port}", flush=True)
    HTTPServer((args.host, args.port), Handler).serve_forever()
