"""Default bridge preflight and namespace-local egress policy primitives."""
from __future__ import annotations

import ipaddress
import json
import re
import subprocess


def validate_network_config(value):
    defaults = {"enabled": False, "docker_host": "unix:///var/run/docker.sock",
                "build_network": "default", "runtime_mode": "isolated",
                "model_host_addresses": {}}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError("invalid docker_network fields")
    config = {**defaults, **value}
    if type(config["enabled"]) is not bool:
        raise ValueError("docker_network.enabled must be boolean")
    if not isinstance(config["docker_host"], str) or not config["docker_host"].startswith("unix:///"):
        raise ValueError("docker_network requires a local Unix Docker socket")
    if config["build_network"] != "default" or config["runtime_mode"] != "isolated":
        raise ValueError("build network must be default; runtime must be isolated")
    hosts = config["model_host_addresses"]
    if not isinstance(hosts, dict):
        raise ValueError("model_host_addresses must be an object")
    for host, address in hosts.items():
        if not isinstance(host, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", host):
            raise ValueError("invalid model hostname")
        ipaddress.ip_address(address)
    return config


def egress_rules(addresses):
    rules = ["delete table inet gost_egress", "table inet gost_egress {",
             "chain egress { type filter hook output priority filter; policy drop;",
             'oifname "lo" accept']
    for address in sorted(set(addresses)):
        ip = ipaddress.ip_address(address)
        rules.append(f"{'ip' if ip.version == 4 else 'ip6'} daddr {ip} tcp dport 443 accept")
    return "\n".join([*rules, "reject", "}", "}"])


def bridge_preflight(config, *, repair=False, runner=subprocess.run):
    """Never restart a daemon or replace firewall rules; repair only a missing link."""
    config = validate_network_config(config)
    docker = ["docker", "--host", config["docker_host"]]
    def run(argv, check=True):
        return runner(argv, capture_output=True, text=True, timeout=15, check=check)
    rows = json.loads(run([*docker, "network", "inspect", "bridge"]).stdout)
    if len(rows) != 1 or rows[0].get("Driver") != "bridge":
        raise ValueError("Docker default bridge metadata is unavailable")
    network = rows[0]
    options = network.get("Options", {})
    name = options.get("com.docker.network.bridge.name", "docker0")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", name):
        raise ValueError("invalid Docker bridge interface")
    ipam = network["IPAM"]["Config"]
    ipv4 = [row for row in ipam if ipaddress.ip_network(row["Subnet"]).version == 4]
    if len(ipv4) != 1:
        raise ValueError("one default bridge IPv4 subnet is required")
    subnet = ipaddress.ip_network(ipv4[0]["Subnet"])
    gateway = ipaddress.ip_address(ipv4[0]["Gateway"])
    if gateway not in subnet:
        raise ValueError("bridge gateway is outside its subnet")
    mtu = options.get("com.docker.network.driver.mtu")
    if mtu and (not str(mtu).isdigit() or not 576 <= int(mtu) <= 9000):
        raise ValueError("invalid bridge MTU")
    link = run(["ip", "-j", "link", "show", "dev", name], check=False)
    repaired = False
    if link.returncode:
        if not repair:
            raise ValueError(f"Docker registered bridge {name}, but its kernel interface is missing; run docker-network --repair after inspecting shared services")
        if network.get("Containers"):
            raise ValueError("bridge repair requires zero attached default-bridge containers")
        # These operations are restricted to the missing interface and its
        # advertised address. Existing interfaces and daemon rules are untouched.
        run(["sudo", "-n", "ip", "link", "add", "name", name, "type", "bridge"])
        run(["sudo", "-n", "ip", "addr", "add", f"{gateway}/{subnet.prefixlen}", "dev", name])
        if mtu:
            run(["sudo", "-n", "ip", "link", "set", "dev", name, "mtu", str(mtu)])
        run(["sudo", "-n", "ip", "link", "set", "dev", name, "up"])
        repaired = True
    links = json.loads(run(["ip", "-j", "-d", "link", "show", "dev", name]).stdout)
    if len(links) != 1 or links[0].get("linkinfo", {}).get("info_kind") != "bridge" or "UP" not in links[0].get("flags", []):
        raise ValueError("default bridge must be an UP Linux bridge")
    addrs = json.loads(run(["ip", "-j", "addr", "show", "dev", name]).stdout)
    if not any(row.get("local") == str(gateway) and row.get("prefixlen") == subnet.prefixlen
               for item in addrs for row in item.get("addr_info", [])):
        raise ValueError("default bridge gateway differs from Docker metadata")
    return {"ok": True, "interface": name, "subnet": str(subnet), "gateway": str(gateway),
            "network_id": network["Id"], "repaired_missing_link": repaired,
            "build_network": "default", "runtime_mode": "isolated"}
