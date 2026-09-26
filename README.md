# opencode-go-usage

OpenCode Go 订阅用量看板：各模型月总量 / 已用 / 剩余 / token，一屏看清，方便按余量切换模型。

![原理](https://img.shields.io/badge/python-stdlib_only-blue) ![部署](https://img.shields.io/badge/docker-官方镜像直跑-green)

## 原理

* 数据源 1：`GET /console/api/v2/usage/export`（v1 已下线）——按 UTC 天汇总的用量 CSV，`cost` 字段即 Go 定价花费，`provider=opencode-go` 圈定订阅用量
* 数据源 2：`GET /console/api/go/limits`——官方限额 live 拉取（本地 `GO_LIMITS.json` 只做 fallback）
* 数据源 3：`GET /zen/go/v1/usage`——官方总体用量（滚动/周/月百分比 + 重置时间），表头直接显示官方口径
* 后端只是带 Key 的转发代理（顺带解决浏览器 CORS），Key 只存你自己手里，不落库

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

如需从外网访问，请自行在前面加一层 HTTPS，且务必设置强 PIN，不要把 8765 裸暴露到公网。

## 口径说明

* 总量 = 官方 Go 月限额；已用 = v2 汇总（排除非 Go provider 行）；剩余 = 总量 − 30d 已用
* 7d ≈ 周，30d ≈ 月，今日（UTC）≈ 5h 窗口；v2 数据可滞后数小时
* 金额为估算，请以 [OpenCode Console](https://opencode.ai/console) 为准

## License

MIT
