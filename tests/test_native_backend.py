from pathlib import Path
import subprocess
import runpy
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from native import backend


class NativeBackendTests(unittest.TestCase):
    def test_saved_upstream_is_used_for_primary_and_probe_and_empty_means_direct(self):
        def reply(*args, **kwargs):
            return SimpleNamespace(returncode=1 if args[0] == "AccountGet" else 0,
                                   stdout="Virtual Network Adapter Name|native\n")

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(backend.vpn_utils, "DATA_DIR", Path(root)), \
                mock.patch.dict(backend.os.environ, {"SOFTETHER_UPSTREAM_PROXY": "socks5://192.0.2.99:1080"}):
            config = Path(root) / "ui_auth.json"
            config.write_text('{"softether_upstream_proxy": "socks5://alice:p%40ss@192.0.2.3:1080"}')
            for probe in (False, True):
                with mock.patch.object(backend, "vpn", side_effect=reply) as vpn:
                    backend.configure_account("gate", "native", "192.0.2.1", 443, probe=probe)
                    vpn.assert_any_call("AccountProxySocks5", "gate", "/SERVER:192.0.2.3:1080", "/USERNAME:alice", "/PASSWORD:p@ss")
            config.write_text('{"softether_upstream_proxy": ""}')
            with mock.patch.object(backend, "vpn", side_effect=reply) as vpn:
                backend.configure_account("gate", "native", "192.0.2.1", 443)
                self.assertFalse(any(call.args[0].startswith("AccountProxy") for call in vpn.call_args_list))
                self.assertTrue(any("/DISABLEUDP:no" in call.args for call in vpn.call_args_list))

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

    @staticmethod
    def ready_command(*args, **kwargs):
        if args[0] == "curl":
            return SimpleNamespace(returncode=0, stdout="203.0.113.5\n")
        if "addr" in args and "show" in args:
            return SimpleNamespace(returncode=0, stdout="inet 10.211.1.2/16")
        return SimpleNamespace(returncode=0, stdout="default via 10.211.254.254")

    def test_preflight_cleans_its_adapter_without_touching_active_routes(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "test.ovpn"
            config.write_text("client\nproto tcp-client\nremote 192.0.2.1 443\n")
            with mock.patch.object(backend, "configure_account"), \
                    mock.patch.object(backend.Session, "connected", return_value=True), \
                    mock.patch.object(backend, "vpn") as vpn, \
                    mock.patch.object(backend, "command", side_effect=self.ready_command) as command, \
                    mock.patch.object(backend.subprocess, "Popen") as dhcp:
                dhcp.return_value.poll.return_value = None
                ok, _, session = backend.run_until_ready(str(config), False, 12, "tun10")
        self.assertTrue(ok)
        self.assertIsNone(session)
        vpn.assert_any_call("NicDelete", "probetun10", check=False)
        for call in command.call_args_list:
            self.assertNotIn("tun0", call.args)
            self.assertNotIn("100", call.args)
        command.assert_any_call("ip", "route", "flush", "table", "210", check=False)
        curl_calls = [call for call in command.call_args_list if call.args[0] == "curl"]
        self.assertTrue(curl_calls)
        self.assertIn("vpn_probetun10", curl_calls[0].args)
        self.assertIn("AIMILI_ROUTE_TABLE=210", dhcp.call_args.args[0])
        dhcp.return_value.terminate.assert_called_once()

    def test_cancelled_handshake_removes_pending_session(self):
        event = threading.Event()
        event.set()
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "test.ovpn"
            config.write_text("client\nproto tcp-client\nremote 192.0.2.1 443\n")
            with mock.patch.object(backend, "configure_account"), \
                    mock.patch.object(backend.Session, "connected", return_value=False), \
                    mock.patch.object(backend, "command"), \
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
        self.assertEqual(0, session.poll())

    def test_reconnecting_account_is_not_treated_as_a_dead_process(self):
        session = backend.Session("gate", "native")
        with mock.patch.object(session, "connected", return_value=False):
            self.assertIsNone(session.poll())

    def test_handshake_with_broken_exit_is_unavailable_and_cleans_probe(self):
        def fail_exit(*args, **kwargs):
            if args[0] == "curl":
                return SimpleNamespace(returncode=28, stdout="")
            return self.ready_command(*args, **kwargs)

        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "test.ovpn"
            config.write_text("client\nproto tcp-client\nremote 192.0.2.1 443\n")
            with mock.patch.object(backend, "configure_account"), \
                    mock.patch.object(backend.Session, "connected", return_value=True), \
                    mock.patch.object(backend, "vpn") as vpn, \
                    mock.patch.object(backend, "command", side_effect=fail_exit) as command, \
                    mock.patch.object(backend.subprocess, "Popen") as dhcp:
                dhcp.return_value.poll.return_value = None
                ok, message, session = backend.run_until_ready(str(config), False, 12, "tun10")
        self.assertFalse(ok)
        self.assertIn("HTTPS 出口验证失败", message)
        self.assertIsNone(session)
        vpn.assert_any_call("NicDelete", "probetun10", check=False)
        command.assert_any_call("ip", "rule", "del", "oif", "vpn_probetun10", "table", "210", check=False)
        dhcp.return_value.terminate.assert_called_once()

    def test_probe_dhcp_uses_its_own_routing_table(self):
        env = {
            "interface": "vpn_probetun10", "reason": "BOUND", "AIMILI_ROUTE_TABLE": "210",
            "new_ip_address": "10.211.1.2", "new_subnet_mask": "255.255.0.0",
            "new_routers": "10.211.254.254",
        }
        with mock.patch.dict(backend.os.environ, env, clear=True), \
                mock.patch.object(subprocess, "run") as run:
            runpy.run_path(str(Path(__file__).parents[1] / "native" / "dhcp.py"))
        routes = [c.args[0] for c in run.call_args_list if "route" in c.args[0]]
        self.assertEqual(2, len(routes))
        for args in routes:
            self.assertIn("210", args)
            self.assertNotIn("100", args)

    def test_retained_probe_keeps_dhcp_and_adapter_until_explicit_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "test.ovpn"
            config.write_text("client\nproto tcp-client\nremote 192.0.2.1 443\n")
            with mock.patch.object(backend, "configure_account"), \
                    mock.patch.object(backend.Session, "connected", return_value=True), \
                    mock.patch.object(backend, "vpn") as vpn, \
                    mock.patch.object(backend, "command", side_effect=self.ready_command), \
                    mock.patch.object(backend.subprocess, "Popen") as dhcp:
                dhcp.return_value.poll.return_value = None
                ok, _, session = backend.run_until_ready(str(config), False, 12, "tun10", retain=True)
                self.assertTrue(ok)
                self.assertIsNotNone(session)
                self.assertFalse(session.stopped)
                dhcp.return_value.terminate.assert_not_called()
                self.assertFalse(any(c.args[0] == "NicDelete" for c in vpn.call_args_list))
                session.terminate()
                dhcp.return_value.terminate.assert_called_once()
                vpn.assert_any_call("NicDelete", "probetun10", check=False)
