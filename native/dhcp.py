#!/usr/bin/python3
"""Configure only the SoftEther adapter and its dedicated routing table."""
import ipaddress
import os
import subprocess


def ip(*args):
    subprocess.run(["ip", *args], check=True)


interface = os.environ["interface"]
reason = os.environ.get("reason", "")
if reason == "PREINIT":
    ip("link", "set", interface, "up")
elif reason in {"BOUND", "RENEW", "REBIND", "REBOOT"}:
    address = ipaddress.IPv4Interface(
        f"{os.environ['new_ip_address']}/{os.environ['new_subnet_mask']}"
    )
    gateway = str(ipaddress.IPv4Address(os.environ["new_routers"].split()[0]))
    old_address = os.environ.get("old_ip_address")
    if old_address and old_address != str(address.ip):
        ip("-4", "addr", "flush", "dev", interface)
    ip("addr", "replace", str(address), "dev", interface)
    ip("route", "replace", str(address.network), "dev", interface, "table", "100")
    ip("route", "replace", "default", "via", gateway, "dev", interface, "table", "100")
    print(f"SoftEther DHCP: {address}, gateway {gateway}", flush=True)
elif reason in {"EXPIRE", "FAIL", "RELEASE", "STOP"}:
    ip("-4", "addr", "flush", "dev", interface)
    # A new container may receive a DHCPNAK before table 100 exists.
    subprocess.run(["ip", "route", "flush", "table", "100"], check=False, capture_output=True)
