"""OpenCode Go 用量看板 - 本地代理后端 (仅标准库, 无需 pip 安装).

运行:  python server.py [--port 8765]
打开:  http://localhost:8765

前端把 Service Account Key (oc_sk_...) 放在请求头 X-Api-Key 发给本代理,
代理再以 Authorization: Bearer 转调官方 Console, 避免浏览器 CORS 和 Key 裸奔到第三方.
也支持服务端预置: set CONSOLE_API_KEY=oc_sk_... (或写 .env 文件).

记账口径 (2026-10 实测逆向, 详见 README):
  官方三个池 (Go 套餐): 5h-from-first-use $12 / UTC 日历周 $30 / 付费周期 $60
  单模型月限额 L **不是独立桶, 而是权重**:
      在模型 i 上花 $X, 会消耗池 $X * (period / L_i)
  => 官方月度% = Σ(花费_i / L_i) * 100
     官方周   = 周内 Σ * (period / week) * 100
     官方滚动 = 5h 内 Σ * (period / five_h) * 100
  所以"总共花了多少钱"和"额度用了百分之几"永远对不上 —— 前者没有额度含义.

  表格的口径必须锚在付费周期: /api/usage/models 传 since=go/status 的 access.startsAt,
  否则不传筛选参数会返回跨周期窗口, 续订后表格仍是上个月的数据 (2026-10-22 踩过).
"""
import datetime
import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONSOLE_URL = os.environ.get("CONSOLE_URL", "https://opencode.ai/console").rstrip("/")
ZEN_URL = os.environ.get("ZEN_URL", "https://opencode.ai/zen").rstrip("/")
DEFAULT_PORT = int(os.environ.get("PORT", "8765"))

# Go 套餐三个池 (美元). 兜底值, 可被 GO_LIMITS.json 的 pool 字段覆盖.
# 来源: Console 前端 bundle 里的 usageLimits 常量
#   go      -> fiveHoursFromFirstUse 12 / utcCalendarWeek 30 / paidPeriod 60
#   go-plus -> 48 / 120 / 240
DEFAULT_POOL = {"five_h": 12.0, "week": 30.0, "period": 60.0}

MICRO_PER_USD = 100_000_000.0  # cost_micro_cents -> USD (一亿, 不是一百万)


def _self_hash():
    """server.py 自身的短哈希。index.html 每次请求都重新读盘, 但 server.py 是启动时
    载入内存的 —— 改了不重启就不会生效, 而 Windows 上 HTTPServer 默认
    allow_reuse_address, 第二个实例绑同一端口不报错, 于是旧进程继续应答,
    表现为"改了但页面没变" (踩过两次)。页脚显示这个哈希, 一眼就能对上。"""
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:8]
    except OSError:
        return "unknown"


BUILD = _self_hash()
STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

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
    ZEN_URL = os.environ.get("ZEN_URL", ZEN_URL).rstrip("/")


def load_limits():
    with open(os.path.join(BASE_DIR, "GO_LIMITS.json"), encoding="utf-8") as f:
        return json.load(f)


def norm_model(s):
    return (s or "").strip().lower()


def usd(micro):
    """micro-cents -> USD."""
    try:
        return float(micro or 0) / MICRO_PER_USD
    except (TypeError, ValueError):
        return 0.0


def iso_z(s):
    """把时间归一成官方 since 只认的形式: YYYY-MM-DDTHH:MM:SS(.mmm)?Z.

    官方 schema 只接受带 Z 的 UTC, 带偏移的会 400, 所以这里统一转 UTC 并去掉毫秒.
    无法解析就返回 None —— 调用方据此降级为"不带 since"的旧行为.
    """
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _api_get(api_key, path, params, accept, base=None):
    """调 Console/Zen API, 返回文本. 失败时抛出带状态码的异常."""
    url = (base or CONSOLE_URL) + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + api_key,
        "Accept": accept,
        "User-Agent": "go-usage-viewer/2.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"官方接口 {e.code}: {body}")


def fetch_usage_models(api_key, page_size=100, since=None):
    """官方 per-model 用量, 不分 provider 混在一起返回.

    GET /console/api/usage/models —— 查询参数: range?(24h/7d/30d/all) / since?(ISO, 必须带 Z)
    / page / pageSize / costOrder.

    **不传筛选参数 ≠ 当前付费周期。** 2026-10-22 续订当天实测: 顶部 go/status 已归零
    ($0.08), 这里不带 since 却仍返回上一周期用量 (Σ $39.41), 对账条于是误报
    "限额表过期/差异较大"。所以必须显式传 since = 付费周期起点
    (/api/go/status 的 access.startsAt), 表格才和官方三窗口同口径。

    必须翻页: pageSize 默认只有 10, 大账号会静默丢数据 (踩过).
    """
    items, page = [], 1
    while True:
        params = {"pageSize": page_size, "page": page}
        if since:
            params["since"] = since
        data = json.loads(_api_get(api_key, "/api/usage/models", params,
                                   "application/json"))
        items.extend(data.get("items") or [])
        info = data.get("pageInfo") or {}
        if page >= (info.get("pageCount") or 1) or page >= 50:  # 安全阀
            break
        page += 1
    return items


def fetch_zen_usage(api_key):
    """官方三窗口百分比: {rolling, weekly, monthly: {status, percent, resetsAt}}.

    只有取整后的百分比; fetch_go_status 可用时就轮不到它了, 这里作兜底."""
    data = json.loads(_api_get(api_key, "/go/v1/usage", {}, "application/json", ZEN_URL))
    return data.get("usage")


def fetch_go_status(api_key):
    """Console Go 页同款订阅状态: 三窗口精确 limit/used (micro-cents) + 付费周期起止.

    GET /console/api/go/status —— Console 自己调的接口, Service Account Key 可用.
    这是唯一能拿到官方"池子到底花了多少美元"的地方 (zen 只给取整百分比).
    Console 前端的百分比公式 (assets/meters-*.js 的 Gm()) 就是 round(used/limit*100).
    失败时由调用方吞掉, 回退到 zen.
    """
    data = json.loads(_api_get(api_key, "/api/go/status", {}, "application/json"))
    access = data.get("access") or {}
    meters = access.get("meters") or {}

    def one(key):
        m = meters.get(key) or {}
        limit = usd(m.get("limitMicroCents"))
        if limit <= 0:
            return None
        used = usd(m.get("usedMicroCents"))
        return {"limit_usd": round(limit, 4), "used_usd": round(used, 4),
                "remaining_usd": round(limit - used, 4),
                "pct": round(used / limit * 100, 2),
                "starts_at": m.get("startsAt"), "resets_at": m.get("resetsAt")}

    return {"product": data.get("product"),
            "period": {"starts_at": access.get("startsAt"), "ends_at": access.get("endsAt")},
            "five_hour": one("fiveHour"), "week": one("week"), "month": one("month")}


def _new_agg():
    return {"cost": 0.0, "requests": 0, "tok_in": 0, "tok_out": 0, "tok_cache": 0}


def build_rows(usage_items, models):
    """把官方用量按模型聚合, 再和限额表算「占额度 %」.

    返回 (rows, unknown, local_pct). local_pct 是各行 quota_pct 之和 ——
    它应当约等于官方月度百分比, 也是对限额表是否过期最直接的自检。
    """
    by_model = {}
    for it in usage_items:
        if (it.get("provider") or "").strip() != "opencode-go":
            continue  # BYOK / 其他 provider 不占 Go 额度
        mid = norm_model(it.get("model"))
        if not mid:
            continue
        a = by_model.setdefault(mid, _new_agg())
        a["cost"] += usd(it.get("totalCostMicroCents"))
        a["requests"] += int(float(it.get("totalRequests") or 0))
        a["tok_in"] += int(float(it.get("totalInputTokens") or 0))
        a["tok_out"] += int(float(it.get("totalOutputTokens") or 0))
        a["tok_cache"] += int(float(it.get("totalCacheReadTokens") or 0))
        a["tok_cache"] += int(float(it.get("totalCacheWrite5mTokens") or 0))
        a["tok_cache"] += int(float(it.get("totalCacheWrite1hTokens") or 0))

    rows, local_pct, known = [], 0.0, set()
    for m in models:
        mid = norm_model(m.get("id"))
        known.add(mid)
        a = by_model.get(mid, _new_agg())
        raw_limit = m.get("monthly_limit_usd")
        unlimited = raw_limit is None
        limit = None if unlimited else float(raw_limit)
        quota_pct = None
        if not unlimited and limit and limit > 0:
            quota_pct = a["cost"] / limit * 100.0
            local_pct += quota_pct
        rows.append({
            "id": m["id"],
            "display": m.get("display", m["id"]),
            "unlimited": unlimited,
            "monthly_limit": limit,
            "quota_pct": None if quota_pct is None else round(quota_pct, 2),
            "spent_usd": round(a["cost"], 4),
            "requests": a["requests"],
            "tok_in": a["tok_in"],
            "tok_out": a["tok_out"],
            "tok_cache": a["tok_cache"],
            "tok_total": a["tok_in"] + a["tok_out"] + a["tok_cache"],
            "note": m.get("note", ""),
            "stale": bool(m.get("stale")),
        })

    # 有限额表之外、但确实花了额度的模型: 单独列出提醒补表
    unknown = [{"id": mid, "spent_usd": round(a["cost"], 4), "requests": a["requests"]}
               for mid, a in by_model.items() if mid not in known and a["cost"] > 0]
    unknown.sort(key=lambda r: -r["spent_usd"])
    rows.sort(key=lambda r: (r["quota_pct"] is None, -(r["quota_pct"] or 0)))
    return rows, unknown, local_pct


class Handler(BaseHTTPRequestHandler):
    server_version = "GoUsageViewer/2.0"

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-Api-Key, X-Access-Pin, Content-Type")
        # 禁止缓存: 之前没发这些头, 浏览器按启发式缓存 index.html,
        # 改完文件刷新还是旧页面 (踩过), 务必保留.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
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
                req = urllib.request.Request(ZEN_URL + "/go/v1/models",
                                             headers={"User-Agent": "go-usage-viewer/2.0"})
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
            try:
                limits = load_limits()
                # 先拿订阅状态: 付费周期起点是下面 per-model 查询的 since ——
                # 顺序不能反, 否则表格会拿到跨周期的用量 (续订后对不上官方数字).
                try:
                    meters = fetch_go_status(api_key)   # 首选: 精确到分的金额
                except Exception:  # noqa: BLE001
                    meters = None
                usage_since = iso_z(((meters or {}).get("period") or {}).get("starts_at"))
                rows, unknown, local_pct = build_rows(
                    fetch_usage_models(api_key, since=usage_since),
                    limits.get("models") or [])
                try:
                    zen = fetch_zen_usage(api_key)      # 兜底: 只有取整百分比
                except Exception:  # noqa: BLE001
                    zen = None

                # 池子大小: 接口给的真实限额 > GO_LIMITS.json > 内置兜底
                pool = limits.get("pool") or DEFAULT_POOL
                if meters:
                    got = {k: (meters.get(k) or {}).get("limit_usd")
                           for k in ("five_hour", "week", "month")}
                    if all(got.values()):
                        pool = {"five_h": got["five_hour"], "week": got["week"],
                                "period": got["month"]}

                # 自检: 逐模型 quota_pct 之和应当约等于官方付费周期用量。
                # 这个对账做不到精确 —— 限额中途被调整过、分档定价、峰谷价
                # 都会造成几个 pp 的偏差, 所以只分档提示, 不下"表已过期"的定论。
                reconcile = None
                month = (meters or {}).get("month") or {}
                if month.get("pct") is not None:
                    official_pct, official_usd = month["pct"], month["used_usd"]
                else:
                    pct = ((zen or {}).get("monthly") or {}).get("percent")
                    official_pct = None if pct is None else float(pct)
                    official_usd = None
                if official_pct is not None:
                    delta = local_pct - official_pct
                    level = ("match" if abs(delta) <= 3
                             else "minor" if abs(delta) <= 10 else "stale")
                    reconcile = {"local_pct": round(local_pct, 1),
                                 "official_pct": round(official_pct, 1),
                                 "delta_pp": round(delta, 1),
                                 "level": level}
                    if official_usd is not None and pool.get("period"):
                        reconcile["local_usd"] = round(local_pct / 100.0 * float(pool["period"]), 2)
                        reconcile["official_usd"] = round(float(official_usd), 2)

                self._send(200, json.dumps({
                    "rows": rows,
                    "unknown_models": unknown,
                    "meters": meters,
                    "zen": zen,
                    "reconcile": reconcile,
                    "pool": pool,
                    # 表格实际用的统计起点。null = 拿不到官方周期信息, 已降级为
                    # "不带 since" 的旧行为 —— 跨周期时表格数字会偏大, 前端要提醒.
                    "usage_since": usage_since,
                    "limits_synced_at": limits.get("synced_at"),
                    "build": BUILD,
                    "started_at": STARTED_AT,
                    "warn": "官方三窗口按「单模型限额加权」计算, 不是美元直加。"
                            "表格的「占额度 %」逐项相加 ≈ 官方付费周期用量, 花费列仅供参考、无额度含义。",
                }))
            except RuntimeError as e:
                self._send(502, json.dumps({"error": str(e)}))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *args):
        pass  # 保持控制台干净


def _port_taken(host, port):
    probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((probe, port)) == 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", str(DEFAULT_PORT))))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    args = ap.parse_args()

    # 端口已被占用时直接退出: HTTPServer 默认 allow_reuse_address, 在 Windows 上
    # 第二个实例能静默绑上同一端口, 结果是旧进程继续应答, 改动看起来没生效。
    if _port_taken(args.host, args.port):
        raise SystemExit(
            "端口 " + str(args.port) + " 已被占用 —— 多半是上一份 server.py 还在跑(旧代码), "
            "改了 server.py 也不会生效。\n先结束旧进程再启动:\n"
            '  Get-CimInstance Win32_Process -Filter "Name=\'python.exe\'" | '
            'Where-Object { $_.CommandLine -match "server.py" } | '
            'ForEach-Object { Stop-Process -Id $_.ProcessId -Force }')

    print(f"Go 用量看板运行中: http://{args.host}:{args.port}"
          f"  (build {BUILD}, pid {os.getpid()}, 启动 {STARTED_AT})", flush=True)
    HTTPServer((args.host, args.port), Handler).serve_forever()
