from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from native import backend


class NativeBackendTests(unittest.TestCase):
    def test_reconnect_keeps_socks5_upstream_and_disables_direct_udp(self):
        def reply(*args, **kwargs):
            return SimpleNamespace(
                returncode=1 if args[0] == "AccountGet" else 0,
                stdout="Virtual Network Adapter Name|native\n",
            )

        with mock.patch.dict(backend.os.environ, {
            "SOFTETHER_UPSTREAM_PROXY": "socks5://10.220.10.253:1081",
        }), mock.patch.object(backend, "vpn", side_effect=reply) as vpn:
            backend.configure_account("gate", "native", "192.0.2.1", 443)
        vpn.assert_any_call("AccountProxySocks5", "gate", "/SERVER:10.220.10.253:1081",
                            "/USERNAME:", "/PASSWORD:")
        self.assertTrue(any("/DISABLEUDP:yes" in call.args for call in vpn.call_args_list))
        self.assertFalse(any(call.args[0] in {"NicCreate", "NicGet"} for call in vpn.call_args_list))

    def test_preflight_cleans_its_adapter_without_touching_active_routes(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "test.ovpn"
            config.write_text("client\nproto tcp-client\nremote 192.0.2.1 443\n")
            with mock.patch.object(backend, "configure_account"), \
                    mock.patch.object(backend.Session, "connected", return_value=True), \
                    mock.patch.object(backend, "vpn") as vpn, \
                    mock.patch.object(backend, "command") as command:
                ok, _, session = backend.run_until_ready(str(config), False, 12, "tun10")
        self.assertTrue(ok)
        self.assertIsNone(session)
        vpn.assert_any_call("NicDelete", "probetun10", check=False)
        command.assert_not_called()

    def test_cancelled_handshake_removes_pending_session(self):
        event = threading.Event()
        event.set()
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "test.ovpn"
            config.write_text("client\nproto tcp-client\nremote 192.0.2.1 443\n")
            with mock.patch.object(backend, "configure_account"), \
                    mock.patch.object(backend.Session, "connected", return_value=False), \
                    mock.patch.object(backend, "vpn") as vpn:
                ok, message, session = backend.run_until_ready(str(config), False, 12, "tun11", event)
        self.assertFalse(ok)
        self.assertIn("取消", message)
        self.assertIsNone(session)
        vpn.assert_any_call("AccountDisconnect", "probe_tun11", check=False)
        vpn.assert_any_call("NicDelete", "probetun11", check=False)

    def test_disconnect_stops_dhcp_before_clearing_network(self):
        session = backend.Session("gate", "native")
        session.dhcp = mock.Mock()
        session.dhcp.poll.return_value = None
        session.dhcp.wait.side_effect = [subprocess.TimeoutExpired("dhclient", 5), 0]
        with mock.patch.object(backend, "vpn") as vpn, mock.patch.object(backend, "command") as command:
            session.terminate()
            session.terminate()
        session.dhcp.terminate.assert_called_once()
        session.dhcp.kill.assert_called_once()
        vpn.assert_any_call("AccountDisconnect", "gate", check=False)
        command.assert_any_call("ip", "-4", "addr", "flush", "dev", "tun0", check=False)
        self.assertEqual(1, session.poll())
