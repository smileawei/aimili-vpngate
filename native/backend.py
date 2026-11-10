"""SoftEther sessions for the manager's connection and preflight lifecycle."""
from pathlib import Path
import ipaddress
import os
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit

import vpn_utils


def command(*args, check=True):
    result = subprocess.run(args, text=True, capture_output=True,
                            stdin=subprocess.DEVNULL, timeout=15)
    if check and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result


def vpn(*args, check=True):
    return command("vpncmd", "localhost", "/CLIENT", "/CMD", *args, check=check)


def start():
    Path("/run/softether").mkdir(parents=True, exist_ok=True)
    command("vpnclient", "start")
    for _ in range(30):
        if vpn("NicList", check=False).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("SoftEther VPN Client 启动失败")


def configure_account(name, nic, host, port, *, probe=False):
    if vpn("AccountGet", name, check=False).returncode == 0:
        vpn("AccountDisconnect", name, check=False)
        vpn("AccountDelete", name)
    adapters = vpn("NicList").stdout
    existing = {line.split("|", 1)[1].strip() for line in adapters.splitlines()
                if line.startswith("Virtual Network Adapter Name|")}
    if nic not in existing:
        vpn("NicCreate", nic)
    vpn("AccountCreate", name, f"/SERVER:{host}:{port}", "/HUB:VPNGATE",
        "/USERNAME:vpn", f"/NICNAME:{nic}")
    vpn("AccountPasswordSet", name, "/PASSWORD:vpn", "/TYPE:standard")
    upstream = os.environ.get("SOFTETHER_UPSTREAM_PROXY", "").strip()
    if upstream:
        proxy = urlsplit(upstream)
        methods = {"http": "AccountProxyHttp", "socks4": "AccountProxySocks",
                   "socks5": "AccountProxySocks5", "socks5h": "AccountProxySocks5"}
        if (proxy.scheme not in methods or not proxy.hostname or not proxy.port
                or proxy.path not in ("", "/") or proxy.query or proxy.fragment):
            raise ValueError("SOFTETHER_UPSTREAM_PROXY 必须包含有效协议、地址和端口")
        proxy_host = f"[{proxy.hostname}]" if ":" in proxy.hostname else proxy.hostname
        vpn(methods[proxy.scheme], name, f"/SERVER:{proxy_host}:{proxy.port}",
            f"/USERNAME:{unquote(proxy.username or '')}",
            f"/PASSWORD:{unquote(proxy.password or '')}")
    vpn("AccountDetailSet", name, f"/MAXTCP:{1 if probe else 8}", "/INTERVAL:1",
        "/TTL:0", "/HALF:no", "/BRIDGE:no", "/MONITOR:no", "/NOTRACK:yes",
        "/NOQOS:no", f"/DISABLEUDP:{'yes' if upstream else 'no'}")


class Session:
    is_softether_session = True

    def __init__(self, name, nic, *, probe=False):
        self.name = name
        self.nic = nic
        self.probe = probe
        self.interface = f"vpn_{nic}" if probe else "tun0"
        self.route_table = str(200 + int(nic.removeprefix("probetun"))) if probe else "100"
        self.dhcp = None
        self.stopped = False
        self._checked_at = 0.0
        self._connected = False
        self._lock = threading.RLock()

    def connected(self):
        with self._lock:
            if self.stopped:
                return False
            if time.monotonic() - self._checked_at > 1:
                result = vpn("AccountStatusGet", self.name, check=False)
                self._connected = result.returncode == 0 and "Session Established" in result.stdout
                self._checked_at = time.monotonic()
            return self._connected

    def poll(self):
        # A reconnecting SoftEther account is still alive. Let exit-health
        # failures cross the manager's threshold before replacing the account.
        return 0 if self.stopped else None

    def terminate(self):
        with self._lock:
            if self.stopped:
                return
            self.stopped = True
            if self.dhcp is not None and self.dhcp.poll() is None:
                self.dhcp.terminate()
                try:
                    self.dhcp.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.dhcp.kill()
                    self.dhcp.wait(timeout=5)
            vpn("AccountDisconnect", self.name, check=False)
            vpn("AccountDelete", self.name, check=False)
            command("ip", "rule", "del", "oif", self.interface, "table", self.route_table, check=False)
            command("ip", "route", "flush", "table", self.route_table, check=False)
            if self.probe:
                vpn("NicDelete", self.nic, check=False)
            else:
                command("ip", "-4", "addr", "flush", "dev", "tun0", check=False)

    kill = terminate

    def wait(self, timeout=None):
        return 0 if self.stopped else self.poll()


def run_until_ready(config_file, keep_alive, timeout, dev, cancel_event=None):
    content = Path(config_file).read_text(encoding="utf-8")
    host, port, proto = vpn_utils.parse_remote(content, "")
    if "tcp" not in proto:
        return False, "该记录只有 OpenVPN UDP 端口，无法用于 SoftEther SSL-VPN", None
    name = "gate" if keep_alive else f"probe_{dev}"
    nic = "native" if keep_alive else f"probe{dev}"
    session = Session(name, nic, probe=not keep_alive)
    deadline = time.monotonic() + timeout
    try:
        configure_account(name, nic, host, port, probe=not keep_alive)
        if keep_alive:
            if Path("/sys/class/net/vpn_native").exists():
                command("ip", "link", "set", "vpn_native", "down")
                command("ip", "link", "set", "vpn_native", "name", "tun0")
            command("ip", "link", "set", "tun0", "up")
        vpn("AccountConnect", name)
        while not session.connected():
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("连接操作已取消")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"SoftEther SSL-VPN 握手超时: {host}:{port}")
            time.sleep(0.25)
        interface, table = session.interface, session.route_table
        command("ip", "link", "set", interface, "up")
        command("ip", "rule", "del", "oif", interface, "table", table, check=False)
        command("ip", "rule", "add", "oif", interface, "table", table)
        # Probe adapters receive separate DHCP state and routing tables; they
        # must never replace tun0's address, lease, or default route.
        session.dhcp = subprocess.Popen([
            "dhclient", "-4", "-d", "-v",
            "-e", f"AIMILI_ROUTE_TABLE={table}",
            "-pf", f"/run/softether-{name}.pid",
            "-lf", f"/tmp/softether-{name}.leases",
            "-sf", "/opt/native/dhcp.py", interface,
        ], stdin=subprocess.DEVNULL)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("连接操作已取消")
            address = command("ip", "-4", "addr", "show", "dev", interface).stdout
            route = command("ip", "route", "show", "table", table, check=False).stdout
            if "inet " in address and "default via " in route:
                if not keep_alive:
                    for url in ("https://api.ipify.org", "https://ip.sb"):
                        if cancel_event is not None and cancel_event.is_set():
                            raise RuntimeError("连接操作已取消")
                        remaining = min(5, deadline - time.monotonic())
                        if remaining <= 0:
                            break
                        result = command(
                            "curl", "-4", "-fsS", "--proxy", "", "--noproxy", "*",
                            "--interface", interface, "--max-time", str(remaining), url,
                            check=False,
                        )
                        if result.returncode == 0:
                            try:
                                ipaddress.ip_address(result.stdout.strip())
                            except ValueError:
                                continue
                            return True, "SoftEther SSL-VPN 握手、DHCP 与 HTTPS 出口验证成功", None
                    raise RuntimeError("SoftEther 隧道已建立但 HTTPS 出口验证失败")
                return True, "SoftEther SSL-VPN 已连接，DHCP 与策略路由就绪", session
            if session.dhcp.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("SoftEther DHCP 地址获取失败或超时")
            time.sleep(0.25)
    except Exception as exc:
        session.terminate()
        return False, str(exc), None
    finally:
        if not keep_alive:
            session.terminate()
