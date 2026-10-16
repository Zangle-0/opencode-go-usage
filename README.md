# opencode-go-usage

OpenCode Go 订阅用量看板：各模型额度占用、花费、token，一屏看清，方便按余量切换模型。

![原理](https://img.shields.io/badge/python-stdlib_only-blue) ![部署](https://img.shields.io/badge/docker-官方镜像直跑-green)

## 核心口径：额度不是钱

**Go 的限额不是一个大池子，而是按模型限额加权的。** 官方三个窗口（Go 套餐，实测取自 `/console/api/go/status`）：

| 窗口 | 池 | 重置 |
| --- | --- | --- |
| 5 小时（从首次使用起算） | $12 | 用完 5h 后释放（`startsAt` 是首次使用时刻） |
| UTC 日历周（周一 00:00Z 起） | $30 | 每周一 00:00 UTC |
| 付费周期（订阅周年） | $60 | 订阅满月那天（界面上显示实际起止） |

单模型月限额 `L` **不是独立额度，而是权重**：在模型 i 上花 $X，消耗池 `X × (period / L_i)`。
等价地：

```
官方付费周期 % = Σ(该模型花费 ÷ 该模型月限额 L) × 100
官方周 %       = 周内 Σ × (period / week) × 100
官方滚动 %     = 5h 内 Σ × (period / five_h) × 100
```

**所以「总共花了多少钱」和「额度占了多少」永远对不上。** 举例：8 个模型合计花了 $20，
但官方记的池子用量是 $36 —— 因为同样 $3，花在 $10 上限的模型上要占 30 个百分点，
花在 $60 上限的模型上只占 5 个百分点。按钱算 $20 ÷ $60 = 33%，真实额度占用是 60%。

界面上表格的 **占额度 %** 列逐项相加，就等于官方付费周期用量 —— 这是对账依据。

### 为什么对账会有几个 pp 的偏差

官方会在不通知的情况下调整单模型限额（例如 `deepseek-v4.1-flash` 在 2026-10 从 $60 上调到 $80）。
如果调整发生在当前付费周期**中间**，周期内较早的用量是按旧限额加权的，此时「本地折算」
和官方金额会差几个美元 —— 这是正常的，不代表限额表过期。周窗口因为只覆盖近期用量，反而更贴近官方值。

所以对账条只分三档：`✓ 一致`（≤3pp）、`≈ 轻微差异`（3–10pp）、`⚠️ 差异较大`（>10pp）。
有 `go/status` 时直接显示美元差（如 `本地折算 $34.80 ｜ 官方 $36.00 ｜ 差 −$1.20`），比百分点直观得多。

## 原理

* 数据源 1：`GET /console/api/go/status`——**官方订阅状态**。三个窗口的精确 `limitMicroCents` /
  `usedMicroCents`，外加付费周期起止。**这是唯一能拿到「官方认为池子花了多少美元」的地方**
  （Console 自己就是调它，前端 `meters.js` 的百分比 = `round(used/limit*100)`）
* 数据源 2：`GET /console/api/usage/models`——**官方 per-model 花费 / token / 请求数**。
  必须翻页，`pageSize` 默认只有 10，不翻页会静默丢模型
* 数据源 3：`GO_LIMITS.json`——单模型限额表，**手工维护**（见下）
* 数据源 4（兜底）：`GET /zen/go/v1/usage`——只有取整百分比，`go/status` 挂掉时才用
* 后端只是带 Key 的转发代理（顺带解决浏览器 CORS），Key 只存你自己手里，不落库

### 怎么刷新限额表

官方文档页 `https://opencode.ai/v2/docs/console/go` 的表格是 **SSR** 的，直接抓 HTML 就能解析：

```python
rows = re.findall(r'<tr><td>([^<]+)</td>.*?<td><strong>([^<]+)</strong></td></tr>', html)
go_limits = rows[:len(rows)//2]   # 前一半是 Go 档，后一半是 Go Plus
```

抓完同步 `synced_at` 字段 —— 页面会显示它，方便判断截图是什么时候的。

## 本地运行

```powershell
python server.py --port 8765
# 浏览器打开 http://localhost:8765，填 Service Account Key（oc_sk_...）查询
```

纯 Python 标准库，无需 `pip install`。

## 服务器部署（Docker，无需 build）

```bash
# 1. 把本目录传到服务器；改 docker-compose.yml 里的 VIEWER_PIN（强 PIN）
# 2. 启动（官方 python:3.12-slim 镜像 + 目录挂载，restart: always 开机自启）
docker compose up -d
# 3. 浏览器打开 http://服务器IP:8765，填 PIN + Key 查询
```

改了 `server.py` / `index.html` / `GO_LIMITS.json` 后 `docker compose restart go-usage` 即可生效，不需要 rebuild。

如需从外网访问，请自行在前面加一层 HTTPS，且务必设置强 PIN，不要把 8765 裸暴露到公网。

### 改了没生效？

`index.html` 每次请求都重新读盘，**但 `server.py` 是启动时载入内存的** —— 改它必须重启进程。
页脚会显示 `服务端 build xxxxxxxx`，那是 `server.py` 的哈希，对不上就是旧进程还在跑。

**Windows 上尤其容易踩**：`HTTPServer` 默认 `allow_reuse_address = 1`，第二个实例**不会报错**，
会静默绑上同一端口，结果是旧代码继续应答 —— 表现为"改了但页面没变"。程序已加启动守卫拦截，
真遇到时先清干净：

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'server.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

浏览器侧不会再吃到旧页面（响应带 `Cache-Control: no-store`），但**已经打开的老标签页仍需
`Ctrl+Shift+R` 强刷一次**。

## 口径说明

* **占额度 %** = 该模型期间花费 ÷ 该模型月限额；逐项相加 ≈ 官方付费周期用量
* **花费** = 官方真实计费，仅供参考，**不能跨模型相加成额度**
* 页面顶部三个蓝条是**官方账号级**数字（精确到分），表格是**按模型拆分的**，两者分母不同
* 模型名后的 **`ⓘ`** 是备注，悬停可看：
  * **琥珀色**＝ 该限额已不在官方文档表中，取自历史快照，可能过期
  * **灰色** ＝ 纯信息（限额变更历史、分档定价说明），影响不到上面的数字
* `GO_LIMITS.json` 里 `monthly_limit_usd: null` = 无限免费模型

## 更新日志

* 2026-10-16：模型名后的标记从 `~`（含义混淆）改成 **`ⓘ` 两色**：琥珀＝限额已不在官方文档表中、可能过期，
  灰色＝纯信息；删掉僵尸条目 `deepseek-flash`，改写全部备注为可读文本。
  另加**启动守卫 + 构建戳**：端口被占直接报错退出（Windows 上 `allow_reuse_address` 会让新旧两个
  server 静默共存、旧代码应答），页脚显示 server.py 的 build 哈希用于核对
* 2026-10-16：**接入 `/console/api/go/status`** —— 顶部三窗口改为显示官方精确金额
  （`$36.00` + 剩余 + 重置倒计时 + 周期起止），不再依赖 zen 的取整百分比；
  对账条直接对比美元（`本地折算 $34.80 ｜ 官方 $36.00 ｜ 差 −$1.20`）；`GO_LIMITS.json` 的 `pool` 降级为兜底
* 2026-10-16：限额表按官方 live 文档全表核对，修正 `deepseek-v4.1-flash` $60 → **$80**（官方已上调），
  补上 `claude-haiku-5-5`($15) 与 `longcat-2.5-preview-free`(无限)，`space-bunny-free` 对齐为 $30；
  对账条从二值判断改为 `一致 / 轻微差异 / 差异较大` 三档（限额中途调整会造成几个 pp 的正常偏差）
* 2026-10-16：**修正核心口径** —— 单模型限额是权重而非独立桶，新增「占额度 %」列与限额表自检对账条；
  数据源从已失效的 `/api/v2/usage/export` + 404 的 `/api/go/limits` 换成 `/console/api/usage/models`（官方 per-model）；
  删掉误导性的「月总量合计 / 月剩余」`今日≈5h` `7d≈周` 三列；`GO_LIMITS.json` 增加 `pool` 与 `synced_at`
* 2026-09-26：表头新增官方总体用量条（`zen/go/v1/usage`，滚动/周/月百分比 + 重置倒计时）
* 2026-09-26：数据源从 v1/export 迁到 v2/export + go/limits live 限额（官方下线 v1）；24h 列改为今日（UTC）；限额表同步到 35 个模型
* 2026-09-23：初版，本地代理 + 网页看板；Docker 部署，PIN 门禁

## License

MIT
