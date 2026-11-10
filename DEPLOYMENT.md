# SoftEther Docker 部署

基于 `smileawei/aimili-vpngate`，保留 OpenVPN 后端，并增加 SoftEther SSL-VPN 后端。

## 构建与启动

```bash
docker build --build-arg BUILD_VERSION=2.1.5 -t aimili-vpngate:2.1.5-local .
docker compose up -d --build softether
docker compose ps
docker compose logs --tail=100 softether
```

宿主机需要 Docker Compose（支持 `include`）、`/dev/net/tun` 和 NET_ADMIN/NET_RAW 能力。
默认 Web 端口为 `8787`，HTTP CONNECT / SOCKS5 共用代理端口 `7929`。
SoftEther 只使用 TCP 候选记录，检测与正式连接均使用相同的上游代理。
当前 Compose 默认上游为 `socks5://10.220.10.253:1081`，可通过
`SOFTETHER_UPSTREAM_PROXY` 环境变量覆盖；使用上游代理时禁用 UDP 加速。

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
