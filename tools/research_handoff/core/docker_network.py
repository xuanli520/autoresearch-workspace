"""Default bridge preflight and scoped model HTTPS forwarding recovery."""
from __future__ import annotations

import ipaddress
import json
import re
import shlex
import subprocess
from typing import Any, Callable, Iterable


def validate_network_config(value: dict[str, Any]) -> dict[str, Any]:
    defaults = {"enabled": False, "docker_host": "unix:///var/run/docker.sock",
                "build_network": "default", "runtime_mode": "isolated",
                "model_host_addresses": {}, "bridge_interface": None,
                "repair_forwarding": False}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError("invalid docker_network fields")
    config = {**defaults, **value}
    if type(config["enabled"]) is not bool:
        raise ValueError("docker_network.enabled must be boolean")
    if type(config["repair_forwarding"]) is not bool:
        raise ValueError("repair_forwarding must be boolean")
    name = config["bridge_interface"]
    if name is not None and (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", name)):
        raise ValueError("invalid expected bridge interface")
    if config["repair_forwarding"] and (not name or name == "docker0"):
        raise ValueError("forwarding repair requires an explicit task bridge")
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


def model_forwarding(config: dict[str, Any], interface: str, subnet: str, *, repair: bool = False,
                     restore_default_forwarding: bool = False,
                     runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> dict[str, Any]:
    def run(argv: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        return runner(argv, capture_output=True, text=True, timeout=15, check=check)

    def iptables(table: str, action: str, chain: str, rule: list[str]) -> bool:
        argv = ["sudo", "-n", "iptables", "-w", "5", "-t", table, action, chain, *rule]
        result = run(argv, check=False)
        if result.returncode not in (0, 1) or action != "-C" and result.returncode:
            raise ValueError(f"cannot inspect or repair bridge forwarding: {result.stderr.strip()}")
        return result.returncode == 0

    if run(["sysctl", "-n", "net.ipv4.ip_forward"]).stdout.strip() != "1":
        raise ValueError("host IPv4 forwarding is disabled; shared sysctl was not changed")
    if restore_default_forwarding and (config["bridge_interface"] != "docker0" or interface != "docker0"):
        raise ValueError("default forwarding restoration requires an explicit docker0 bridge")
    can_repair = repair or restore_default_forwarding
    if repair and (config["bridge_interface"] != interface or interface == "docker0"):
        raise ValueError("forwarding repair requires the exact dedicated task bridge")
    repaired = []
    nat = ["-s", subnet, "!", "-o", interface, "-j", "MASQUERADE"]
    if not iptables("nat", "-C", "POSTROUTING", nat):
        if not can_repair:
            raise ValueError(f"missing Docker NAT rule for {interface}")
        iptables("nat", "-A", "POSTROUTING", nat)
        repaired.append({"table": "nat", "chain": "POSTROUTING", "rule": nat})
    native = (iptables("filter", "-C", "FORWARD", ["-j", "DOCKER-FORWARD"])
              and iptables("filter", "-C", "DOCKER-FORWARD", ["-i", interface, "-j", "ACCEPT"])
              and iptables("filter", "-C", "DOCKER-CT", ["-o", interface, "-m", "conntrack",
                           "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"]))
    # Restore standard default-bridge egress for builds only on an explicit
    # operator request. Agent restrictions remain in its network namespace.
    # These additive rules cannot match another bridge or change chain policy.
    default_rules = [
        ["-i", interface, "-s", subnet, "!", "-o", interface,
         "-m", "comment", "--comment", "research-handoff:default:" + interface, "-j", "ACCEPT"],
        ["-o", interface, "-d", subnet, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED",
         "-m", "comment", "--comment", "research-handoff:default:" + interface, "-j", "ACCEPT"],
    ]
    default_present = False
    relocated = []
    if interface == "docker0" and not native:
        present = [iptables("filter", "-C", "FORWARD", rule) for rule in default_rules]
        if restore_default_forwarding:
            listed = run(["sudo", "-n", "iptables", "-w", "5", "-t", "filter", "-S", "FORWARD"])
            ordered = [shlex.split(line)[2:] for line in listed.stdout.splitlines() if line.startswith('-A FORWARD ')]
            user_jumps = [i for i, rule in enumerate(ordered) if rule == ['-j', 'DOCKER-USER']]
            def rule_key(rule: list[str]) -> list[tuple[str, str, bool]] | None:
                options, index, inverted = [], 0, False
                while index < len(rule):
                    if rule[index] == '!':
                        inverted = True
                        index += 1
                        continue
                    if rule[index] not in ('-i', '-o', '-s', '-d', '-m', '-j', '--comment', '--ctstate') or index + 1 >= len(rule):
                        return None
                    options.append((rule[index], rule[index + 1], inverted))
                    index += 2
                    inverted = False
                return sorted(options)
            for rule, exists in zip(default_rules, present):
                # A previous release inserted these ahead of DOCKER-USER,
                # bypassing host policy and MSS clamping on small-MTU uplinks.
                positions = [i for i, row in enumerate(ordered) if rule_key(row) == rule_key(rule)]
                if exists and user_jumps and any(i < max(user_jumps) for i in positions):
                    iptables("filter", "-D", "FORWARD", rule)
                    iptables("filter", "-A", "FORWARD", rule)
                    relocated.append({"table": "filter", "chain": "FORWARD", "rule": rule})
                if not exists:
                    iptables("filter", "-A", "FORWARD", rule)
                    repaired.append({"table": "filter", "chain": "FORWARD", "rule": rule})
            default_present = True
        else:
            default_present = all(present)
    if not native and not default_present:
        addresses = sorted(set(config["model_host_addresses"].values()))
        if not addresses or any(ipaddress.ip_address(ip).version != 4 for ip in addresses):
            raise ValueError("missing native Docker forwarding; scoped recovery requires IPv4 model endpoints")
        for address in addresses:
            rules = [
                ["-i", interface, "-s", subnet, "-d", address, "-p", "tcp", "--dport", "443",
                 "-m", "conntrack", "--ctstate", "NEW,ESTABLISHED", "-m", "comment",
                 "--comment", "research-handoff:" + interface, "-j", "ACCEPT"],
                ["-o", interface, "-d", subnet, "-s", address, "-p", "tcp", "--sport", "443",
                 "-m", "conntrack", "--ctstate", "ESTABLISHED", "-m", "comment",
                 "--comment", "research-handoff:" + interface, "-j", "ACCEPT"],
            ]
            for rule in rules:
                if iptables("filter", "-C", "FORWARD", rule):
                    continue
                if not can_repair:
                    raise ValueError(f"missing model HTTPS forwarding for {interface}; explicit scoped repair required")
                iptables("filter", "-I", "FORWARD", ["1", *rule])
                repaired.append({"table": "filter", "chain": "FORWARD", "rule": rule})
    return {"checked": True, "native_docker_forwarding": native,
            "restored_default_forwarding": default_present, "repaired_rules": repaired,
            "relocated_default_rules": relocated}


def egress_rules(addresses: Iterable[str]) -> str:
    rules = ["delete table inet gost_egress", "table inet gost_egress {",
             "chain egress { type filter hook output priority filter; policy drop;",
             'oifname "lo" accept']
    for address in sorted(set(addresses)):
        ip = ipaddress.ip_address(address)
        rules.append(f"{'ip' if ip.version == 4 else 'ip6'} daddr {ip} tcp dport 443 accept")
    return "\n".join([*rules, "reject", "}", "}"])


def dedicated_build_forwarding(config: dict[str, Any], *,
                               runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> dict[str, Any]:
    """Restore package-source access only for a named dedicated default bridge.

    Rules remain behind DOCKER-USER and match the bridge plus its exact subnet.
    This never changes shared chain policies, sysctls or daemon configuration.
    Runtime Agent egress is independently restricted in its network namespace.
    """
    config = validate_network_config(config)
    name = config["bridge_interface"]
    if not config["enabled"] or not config["repair_forwarding"] or not name or name == "docker0":
        raise ValueError("build forwarding requires an explicitly authorized dedicated bridge")

    def run(argv):
        return runner(argv, capture_output=True, text=True, timeout=15, check=False)

    inspected = run(["docker", "--host", config["docker_host"], "network", "inspect", "bridge"])
    if inspected.returncode:
        raise ValueError("cannot inspect dedicated build bridge")
    rows = json.loads(inspected.stdout)
    if len(rows) != 1 or rows[0].get("Driver") != "bridge":
        raise ValueError("one dedicated bridge is required")
    network = rows[0]
    if network.get("Options", {}).get("com.docker.network.bridge.name", "docker0") != name:
        raise ValueError("build bridge identity mismatch")
    ipv4 = [row for row in network["IPAM"]["Config"]
            if ipaddress.ip_network(row["Subnet"]).version == 4]
    if len(ipv4) != 1:
        raise ValueError("one dedicated IPv4 subnet is required")
    subnet = str(ipaddress.ip_network(ipv4[0]["Subnet"]))
    if run(["sysctl", "-n", "net.ipv4.ip_forward"]).stdout.strip() != "1":
        raise ValueError("host forwarding is disabled; shared sysctl was not changed")
    inspected_link = run(["ip", "-j", "-d", "link", "show", "dev", name])
    links = json.loads(inspected_link.stdout) if not inspected_link.returncode else []
    if len(links) != 1 or links[0].get("linkinfo", {}).get("info_kind") != "bridge" or "UP" not in links[0].get("flags", []):
        raise ValueError("dedicated build interface must be an existing UP bridge")
    comment = "research-handoff:build:" + name
    rules = [("nat", "POSTROUTING", ["-s", subnet, "!", "-o", name,
                                    "-m", "comment", "--comment", comment, "-j", "MASQUERADE"])]
    for protocol, ports in [("udp", "53"), ("tcp", "53,80,443")]:
        rules.append(("filter", "FORWARD", ["-i", name, "-s", subnet, "!", "-o", name,
                     "-p", protocol, "-m", "multiport", "--dports", ports,
                     "-m", "conntrack", "--ctstate", "NEW,ESTABLISHED",
                     "-m", "comment", "--comment", comment, "-j", "ACCEPT"]))
    rules.append(("filter", "FORWARD", ["-o", name, "-d", subnet,
                  "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED",
                  "-m", "comment", "--comment", comment, "-j", "ACCEPT"]))
    added = []
    for table, chain, rule in rules:
        prefix = ["sudo", "-n", "iptables", "-w", "5", "-t", table]
        checked = run([*prefix, "-C", chain, *rule])
        if checked.returncode not in (0, 1):
            raise ValueError("cannot inspect dedicated build forwarding")
        if checked.returncode == 1:
            applied = run([*prefix, "-A", chain, *rule])
            if applied.returncode:
                raise ValueError("could not restore dedicated build forwarding")
            added.append({"table": table, "chain": chain, "rule": rule})
    return {"ok": True, "interface": name, "subnet": subnet, "network_id": network["Id"],
            "added_rules": added, "shared_policies_changed": False,
            "daemon_restarted": False, "allowed_build_ports": [53, 80, 443]}


def bridge_preflight(config: dict[str, Any], *, repair: bool = False, repair_forwarding: bool = False,
                     restore_default_forwarding: bool = False,
                     runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> dict[str, Any]:
    """Inspect bridge and forwarding; repairs never restart or flush shared services."""
    config = validate_network_config(config)
    if restore_default_forwarding and (not config["enabled"] or config["bridge_interface"] != "docker0"):
        raise ValueError("default forwarding restoration requires enabled networking and explicit docker0")
    docker = ["docker", "--host", config["docker_host"]]
    def run(argv: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        return runner(argv, capture_output=True, text=True, timeout=15, check=check)
    rows = json.loads(run([*docker, "network", "inspect", "bridge"]).stdout)
    if len(rows) != 1 or rows[0].get("Driver") != "bridge":
        raise ValueError("Docker default bridge metadata is unavailable")
    network = rows[0]
    options = network.get("Options", {})
    name = options.get("com.docker.network.bridge.name", "docker0")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", name):
        raise ValueError("invalid Docker bridge interface")
    if config["bridge_interface"] is not None and config["bridge_interface"] != name:
        raise ValueError("Docker bridge differs from the configured task interface")
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
    forwarding = (model_forwarding(config, name, str(subnet),
                                  repair=repair_forwarding or (repair and name != "docker0"),
                                  restore_default_forwarding=restore_default_forwarding, runner=runner)
                  if config["enabled"] else {"checked": False})
    return {"ok": True, "interface": name, "subnet": str(subnet), "gateway": str(gateway),
            "network_id": network["Id"], "repaired_missing_link": repaired,
            "build_network": "default", "runtime_mode": "isolated", "forwarding": forwarding}
