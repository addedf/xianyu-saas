# 日本服务器生产部署模板

入口为 `https://xianyu.sulian.org/xianyu-saas/`。Cloudflare 代理客户端的 HTTPS
请求，仅对该域名通过 Origin Rule 回源到服务器的 `8443`。既有 AnyTLS 继续占用
`443`。Nginx 转发到 `app:8096`，网页和 API 均由 FastAPI 提供，无需启动 Node。

## 目录与镜像

把本目录的内容上传到服务器的 `/opt/xianyu-saas/deployment/`，保留如下布局：

```text
/opt/xianyu-saas/
├── deployment/
│   ├── compose.yml
│   ├── nginx.conf
│   ├── cloudflare-allowlist.conf   # 部署时从官方源生成
│   └── README.md
├── config/
│   ├── image.env                  # 镜像标签或 digest，不含业务凭据
│   ├── production.env             # 生产配置，0600，不提交 Git
│   └── tls/
│       ├── origin.pem            # 对应该域名的源站证书
│       └── origin-key.pem        # 证书私钥，0600
└── data/                         # 持久数据，应用 UID/GID 10001:10001
    ├── saas.db
    ├── tenants/
    └── ai-master-key
```

`image.env` 必须设置 `XIANYU_IMAGE` 为自己的已构建镜像版本；推荐使用不可变版本标签
或 digest。可用 `NGINX_IMAGE` 固定经过确认的官方 Nginx 镜像；缺省为
`nginx:stable-alpine`。模板不挂载 Docker socket，不安装独立上游更新器。

Compose 相对路径以服务器上的 `deployment/compose.yml` 为基准，不以当前 shell
目录为基准。`--env-file config/image.env` 用于镜像插值；应用业务配置由独立的
`config/production.env` 注入。

## 首次迁移核对阶段

先停本地 API、消费者和店铺 Worker，再迁移一致的主数据库、店铺目录及原主密钥。
`data/` 与镜像分开保存，不能用开发数据覆盖已经继续接单的生产数据库。

在 `config/production.env` 中保留生产所需业务设置，并明确设置：

```dotenv
SAAS_LAUNCH_WEB=0
SAAS_LAUNCH_CONSUMER=0
SAAS_RESTORE_WORKERS=0
SAAS_SELF_UPDATE_ENABLED=0
SAAS_ALLOW_REGISTRATION=0
SAAS_BOOTSTRAP_ENABLED=0
SAAS_MAX_BOTS=1
SAAS_BOT_MEM_MB=400
```

不设置 `SAAS_AI_MASTER_KEY` 时，入口从迁移后的 `data/ai-master-key` 读取；如果原部署
通过环境变量提供主密钥，则必须安全迁移同一把密钥。不要重新生成后覆盖原密钥。

模板固定生产模式、数据库目录、公开域名、HTTPS Cookie 及可信 Nginx 地址。这里
的启动开关由 `production.env` 控制，Compose 不覆盖它们。不要启用 `SAAS_TESTING`。

先只启动 `app`，此时不依赖证书或 Nginx allowlist：

```bash
docker compose --env-file /opt/xianyu-saas/config/image.env \
  -f /opt/xianyu-saas/deployment/compose.yml up -d app
```

核对健康状态、用户/店铺/商品/卡池数量、可用与已消耗库存、发货记录、模型凭据
是否能正常解密，并确认没有消费者或店铺 Worker。API 只发布在宿主
`127.0.0.1:8096`，可通过 SSH 隧道或服务器本机检查。

## Cloudflare 与 TLS

1. 创建指向日本服务器 IPv4 的 `xianyu.sulian.org` A 记录，开启代理（橙云）。
2. SSL/TLS 使用 **Full (strict)**；源站证书必须覆盖该域名。
3. Origin Rule 仅匹配 `(http.host eq "xianyu.sulian.org")`，只覆盖目标端口为
   `8443`。不修改其他域名、Host 或 SNI，浏览器入口仍为标准 HTTPS 443。
   HTTP 请求也可能经该规则发送到源站 8443；Nginx 用 `error_page 497` 统一返回
   308 到固定的 `https://xianyu.sulian.org`，保留原路径与查询串，不读取请求 Host
   生成跳转。此处理在默认虚拟主机与访问白名单阶段之前完成，只返回跳转；业务
   页面仍需完成 TLS 并通过来源白名单。
4. 在该域名设置 Cache Rule 绕过缓存；不要给认证/API 页面套用 Cache Everything。
   Nginx 同时返回 `Cache-Control: no-store`，关闭代理缓存及响应缓冲。
5. 将证书和私钥放入上述 `config/tls/`，Nginx 按只读目录挂载。已有证书可安全复制，
   不把证书私钥写进 Git、镜像或聊天。

来源：[Origin Rules 设置](https://developers.cloudflare.com/rules/origin-rules/features/)。

## 生成 Cloudflare 来源白名单

模板不附带静态 IP 清单。部署时从无需认证的
[Cloudflare 官方 IP API](https://api.cloudflare.com/client/v4/ips)
获取 `result.ipv4_cidrs` 与 `result.ipv6_cidrs`；此前文本列表端点在服务器返回 403。
默认 TLS 校验、响应结构及 CIDR 校验均须成功，才原子替换独立 include；获取失败
或任何一组为空时不覆盖旧文件：

```bash
python3 - <<'PY'
import ipaddress
import json
import os
import tempfile
import urllib.request
from pathlib import Path

destination = Path('/opt/xianyu-saas/deployment/cloudflare-allowlist.conf')
with urllib.request.urlopen('https://api.cloudflare.com/client/v4/ips', timeout=20) as response:
    raw = response.read(65537)
if len(raw) > 65536:
    raise RuntimeError('Cloudflare IP response too large')
payload = json.loads(raw)
if not isinstance(payload, dict) or payload.get('success') is not True:
    raise RuntimeError('Cloudflare IP API request failed')
result = payload.get('result')
if not isinstance(result, dict):
    raise RuntimeError('Invalid Cloudflare IP API result')
networks = []
for family, key in (
    (4, 'ipv4_cidrs'),
    (6, 'ipv6_cidrs'),
):
    values = result.get(key)
    if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
        raise RuntimeError('Invalid Cloudflare CIDR list')
    parsed = [ipaddress.ip_network(value.strip(), strict=True) for value in values]
    if any(network.version != family or network.prefixlen == 0 for network in parsed):
        raise RuntimeError('Invalid Cloudflare IP response')
    networks.extend(parsed)

content = '# Generated from https://api.cloudflare.com/client/v4/ips.\n'
content += ''.join(f'allow {network};\n' for network in networks)
with tempfile.NamedTemporaryFile('w', dir=destination.parent, delete=False) as stream:
    temporary = Path(stream.name)
    stream.write(content)
    stream.flush()
    os.fsync(stream.fileno())
try:
    temporary.chmod(0o644)
    temporary.replace(destination)
finally:
    temporary.unlink(missing_ok=True)
print(f'Generated allowlist with {len(networks)} ranges')
PY
```

Nginx 仅允许这些 Cloudflare 来源、localhost 与 Docker 网关 `172.30.47.1`。
allow/deny 检查使用实际 socket peer；不要再增加 `real_ip_header` 或
`set_real_ip_from`，否则可能先改写 `$remote_addr`，导致白名单与真实来源不一致。
只有已获准来源传入的 `CF-Connecting-IP` 会转为后端 `X-Real-IP` 与
`X-Forwarded-For`；客户端自带的转发链不继续拼接。

8443 只发布在 IPv4，域名使用 A 记录。这样避免 IPv6 经 Docker userland proxy
折叠成获准网关来源；如要开启源站 IPv6，需先配置相应来源防火墙并确认容器
保留真实连接来源。宿主防火墙还可在 Docker 转发链限制 8443 为官方 Cloudflare
网段，不修改 AnyTLS 的 443。不要仅依赖 UFW 的宿主 INPUT 规则来限制 Docker 发布端口。

证书、规则和 allowlist 齐备后再启动 Nginx：

```bash
docker compose --env-file /opt/xianyu-saas/config/image.env \
  -f /opt/xianyu-saas/deployment/compose.yml up -d nginx
```

## 启用接单与后续发版

迁移核对和域名访问成功后，将 `production.env` 的
`SAAS_LAUNCH_CONSUMER=1`、`SAAS_RESTORE_WORKERS=1`，保持
`SAAS_LAUNCH_WEB=0`、`SAAS_SELF_UPDATE_ENABLED=0`，重建应用容器：

```bash
docker compose --env-file /opt/xianyu-saas/config/image.env \
  -f /opt/xianyu-saas/deployment/compose.yml up -d --no-deps --force-recreate app
```

`SAAS_RESTORE_WORKERS=1` 会恢复数据库中期望运行的店铺，启用前应确认店铺状态与
自动回复/发货配置。确认线上已接管后，本地实例保持停止。

发版前备份当前一致数据，更新 `image.env` 为新版本镜像，再重建 `app`。数据库迁移
单独核对；回退镜像不代表可以恢复旧库存数据库。

### SKU 发货版本迁移

先停止 `app` 并备份一致的 `data/` 和配置，再执行独立迁移（不启动 Worker、不访问闲鱼）：

```bash
python3 /opt/xianyu-saas/deployment/migrate-sku.py --data-root /opt/xianyu-saas/data
python3 /opt/xianyu-saas/deployment/migrate-sku.py --data-root /opt/xianyu-saas/data --apply
```

迁移只为已有 `delivery_events` 添加可空 `sku_id`，可重复执行，并核对数据库完整性和
订单、库存状态计数；不会恢复卡密。启动新版本后，通过线上 API 重新同步商品，逐个 SKU
选择线上已有的对应卡池。不要复制本地数据库、卡密清单或配置文件覆盖线上。配置齐备后
保持消费者和 Worker 恢复开关为 `1`，启动目标店铺规则模式，核验 `WS_REGISTERED`。

刷新白名单或替换 `nginx.conf` 后，因单文件 bind mount 的 inode 行为，重建 Nginx
容器读取新文件：

```bash
docker compose --env-file /opt/xianyu-saas/config/image.env \
  -f /opt/xianyu-saas/deployment/compose.yml up -d --no-deps --force-recreate nginx
```

## 资源与排障

- 独立网络 `172.30.47.0/24`：app `.2`，Nginx `.3`，网关 `.1`。部署前确认不与现有网络重叠。
- 应用限额 `768m / 1 CPU`，Nginx `96m / 0.25 CPU`；应用限额包含其派生的消费者与 Worker。
- 两个服务都使用 `unless-stopped`、`no-new-privileges`、90 秒退出宽限期。
- 容器 stdout/stderr 使用 Docker `json-file`，各最多 `10m × 3`；业务数据与历史日志文件需另行监控容量。
- Nginx access log 仅保留时间、连接来源、方法、路径、状态、字节与耗时，不记录查询串、Cookie、请求头或请求体。
  运行期 Nginx error log 关闭，避免其中的完整请求行泄漏查询参数；HTTP 状态与应用错误结合排查。
- Uvicorn 显式 `--no-access-log --no-proxy-headers`，保留 Nginx 的实际 peer。
  后端现有 `_client_address()` 只信任 `172.30.47.3` 的真实客户端头。
  `SAAS_PUBLIC_ORIGIN` 与 `SAAS_COOKIE_SECURE=1` 显式提供公开 HTTPS 配置，内部
  Worker 的 localhost 调用仍保留 loopback 身份。
- 线上排查先记录镜像版本、时间、相关记录 ID 与容器日志；需要本地复现时拉一致
  数据快照，并关闭消费者/Worker/真实发货，不把调试库覆盖回线上。
