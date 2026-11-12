# SoftEther Docker 部署

基于 `smileawei/aimili-vpngate`，保留 OpenVPN 后端，并增加 SoftEther SSL-VPN 后端。

## 构建与启动

```bash
docker compose up -d --build softether
docker compose ps
docker compose logs --tail=100 softether
```

宿主机需要 Docker Compose（支持 `include`）、`/dev/net/tun` 和 NET_ADMIN/NET_RAW 能力。
默认 Web 端口为 `8787`，HTTP CONNECT / SOCKS5 共用代理端口 `7929`。
SoftEther 只使用 TCP 候选记录，检测与正式连接均使用相同的上游代理。
当前源码及 Compose 默认不配置上游代理，直接连接节点。

## Web 配置上游代理

在 Web 后台打开 **代理设置 → 节点连接上游代理**，填写地址后保存，例如
`socks5://192.0.2.10:1080` 或 `http://用户名:密码@主机:端口`。
支持 `http`、`socks4`、`socks5`、`socks5h`，用户名和密码中的特殊字符需要 URL 编码。
修改后服务自动重启，节点检测、主连接和热备统一使用新配置；已有连接会中断并重新建立。
已配置时输入框留空会保留原值；勾选 **清除已配置的上游代理，改为直连** 可恢复直连。
代理地址不会回显，配置保存在挂载目录的 `data/ui_auth.json`，重建容器后保留。

也可通过 `SOFTETHER_UPSTREAM_PROXY` 环境变量提供初始值；Web 保存的设置优先，
Web 清除代理后也不会重新使用环境变量中的地址。使用上游代理时禁用 UDP 加速。
此 Web 功能从 `2.2.1` 开始提供，`2.2.0` 镜像不包含。
从旧版本升级时，如需继续使用原上游，请在 `.env` 显式设置 `SOFTETHER_UPSTREAM_PROXY`，
或升级后在 Web 重新配置；新 Compose 不再附带旧的默认地址。

## 内网免登录入口

首次启动会生成 `data/ui_auth.json`。停止服务后修改其中两项，保留其他配置：

```json
{
  "auth_enabled": false,
  "secret_path": ""
}
```

启动服务后直接访问 `http://服务器IP:8787/`，无需账号、密码或路径后缀。
`auth_enabled` 默认值为 `true`；设为 `false` 后，该 Web 服务的所有管理接口均无需登录。
配置保存在挂载的数据目录，重启、重建容器均会保留。
`data/`、`.env`、凭据及本地工具配置均不提交到 Git。

## 验证与管理

```bash
python3 -m unittest discover -s tests
curl --max-time 20 --proxy socks5h://127.0.0.1:7929 https://api.ipify.org
docker compose restart softether
docker compose logs -f --tail=100 softether
```

健康检查仅验证 Web 服务，VPN 出站状态可在后台查看或通过代理请求验证。
原 OpenVPN 服务保留在 `legacy-openvpn` profile；两个后端应分别部署，避免共享数据目录和监听端口。

## 自动恢复

- `PROXY_CHECK_INTERVAL_SECONDS`：出口守护间隔，SoftEther 默认 1 秒，OpenVPN 默认 15 秒；每轮请求耗时另计。
- `PROXY_FAILURE_THRESHOLD`：连续失败阈值，SoftEther 默认 2，OpenVPN 默认 3；成功一次会清零。SoftEther 短暂重连也遵守该阈值。
- `NODE_EXIT_PROBE_TIMEOUT_SECONDS`：SoftEther 备用检测和切换预检总预算，默认 25 秒，覆盖握手、DHCP 与 HTTPS 出口验证。
- 备用网卡使用独立 DHCP 状态和 202～299 路由表；初始主连接使用 `tun0` 和 table 100，热备提升后保留其独立网卡与路由表。
- 节点列表中的 SoftEther 探测耗时包含握手、DHCP 与出口验证，不是单次 ping RTT；活动节点显示实际代理出口请求耗时。
- 达到失败阈值后先检查共享上游；上游出站或本地代理异常时暂缓换节点及拉黑。
- 单轮最多尝试三个不同备用，之后持续补齐：15、30、60、120、240、300 秒，随后维持 300 秒退避，直到出口恢复或连接禁用。
- 固定 IP 模式仍只重连同一节点，住宅、地区及收藏筛选继续生效。

## 一主一热备（SoftEther）

服务会额外维持一条经过 DHCP 和 HTTPS 验证的备用隧道。主备共用所配置的上游代理，未配置时均直连，分别使用独立的接口和路由表；后台显示热备状态。
发生持续出口故障时，新请求直接使用已就绪的热备，并后台补齐备用，不必重新建立隧道。已有 TCP 会话需要客户端重连。
热备不可用或健康验证超过 5 秒时不会直接提升，而是走普通切换流程；固定 IP 模式停用热备，国家、住宅、收藏限制继续生效。
热切换后的主连接也保留其原有独立路由表（202～299），不强制改名为 tun0；DNS 与代理新连接同步选择当前主接口。
持续主备维护比单连接增加一条隧道及健康检测流量。配置上游代理时，两条隧道共享上游，因此无法覆盖上游本身故障。

## 切换防抖

连续快速检测失败后，SoftEther 会先执行双地址并发、单路最长 3 秒的出口复核。
任一路复核成功即保留当前主连接并清零失败计数；复核失败才进入共享上游排查及切换。
这样会比原热切换增加最多约 3 秒的确认时间，降低短时抖动造成的误切；不承诺所有网络下的固定恢复耗时。
热备连续检查失败时也会通过另一检测地址复核，确认失败才清理会话。

## 使用 GitHub 发布的镜像

主干代码更新后，GitHub Actions 的 **Publish SoftEther image** 会测试并发布：

```text
ghcr.io/smileawei/aimili-vpngate-softether:latest
ghcr.io/smileawei/aimili-vpngate-softether:sha-<12位提交号>
```

支持 `linux/amd64`、`linux/arm64`，Docker 会自动选择对应架构。
首次发布若包默认为私有，在 GitHub 仓库的 Packages 中打开该镜像，进入 Package settings，将 Visibility 设为 Public 后即可匿名拉取。

从仓库目录执行（保留原 `data/`，不要同时运行两套占用相同端口的服务）：

```bash
docker compose -f compose.ghcr.yaml pull
docker compose -f compose.ghcr.yaml up -d
docker compose -f compose.ghcr.yaml ps
```

更新时同样执行 `pull` 和 `up -d`，不需要本地构建。
可在 `.env` 设置 `SOFTETHER_IMAGE_TAG=sha-<12位提交号>` 固定镜像版本；上游默认直连，推荐在 Web「代理设置」配置，也可用 `SOFTETHER_UPSTREAM_PROXY` 提供初始值。
发布工作流使用 GitHub 自动提供的 `GITHUB_TOKEN`，权限为 `contents: read`、`packages: write`。
如果 fork 的 Actions 尚未启用，先在仓库 Actions 页面启用，再手动运行此工作流。

## 查询节点切换记录

在后台“主备连接状态”旁点击 **切换记录**。日期和时间使用浏览器本地时区，可选起止日期、结果并分页。
记录位于挂载数据目录的 `switch_history/YYYY-MM-DD.jsonl`，保留约 90 天，更新镜像或重启不会丢失；备份时包含整个 `data/` 目录即可。
首次更新会导入仍保留的运行日志，旧记录缺少的触发原因和耗时不会猜测补全，已经删除的日志无法恢复。
操作耗时指连接或热备提升本身，不包含之前的故障检测等待。新记录会区分自动、手动、启动恢复和失败后恢复。

## 查看 IP 变化统计

在 **切换记录** 中选择日期范围和 h / day / week 维度。图表仅按日期统计，明细结果筛选不改变图表数据；周一为周起点。
默认仅纳入成功且实测确认出口 IP 改变的操作，同 IP 换端口、失败、首次连接及启动恢复不计入。
新版本会记录前后出口 IP；旧记录无实测数据，默认不计入，可勾选 **包含历史节点 IP 推断** 查看按旧节点地址推断的变化，推断数量会单独说明。
统计时区来自浏览器，镜像显式安装 tzdata 以支持不同地区及夏令时规则。统计仍受 90 天保留期限制。

## V2.2.0 固定版本升级

```bash
SOFTETHER_IMAGE_TAG=2.2.0 docker compose -f compose.ghcr.yaml pull
SOFTETHER_IMAGE_TAG=2.2.0 docker compose -f compose.ghcr.yaml up -d
```

需要后续操作仍固定版本时，编辑现有 `.env`，设置 `SOFTETHER_IMAGE_TAG=2.2.0`。保留 `data/`，不要清空凭据和切换历史。
[本次发布说明](docs/releases/v2.2.0.md)列出了镜像、源码包与兼容性说明。
