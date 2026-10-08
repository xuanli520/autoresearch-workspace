import json
import subprocess
import unittest
from unittest.mock import patch

from tools.research_handoff.core.docker_network import bridge_preflight, dedicated_build_forwarding, egress_rules, model_forwarding, validate_network_config


class BridgeTests(unittest.TestCase):
    def runner(self, attached=False):
        self.commands, self.link_exists = [], False
        network = {'Driver':'bridge','Id':'bridge-id','Containers':{'other':{}} if attached else {},
            'Options':{'com.docker.network.bridge.name':'docker0','com.docker.network.driver.mtu':'1500'},
            'IPAM':{'Config':[{'Subnet':'172.17.0.0/16','Gateway':'172.17.0.1'}]}}
        def run(argv, **kwargs):
            self.commands.append(argv)
            if argv[0] == 'docker':
                output, code = [network], 0
            elif argv[:5] == ['sudo','-n','ip','link','add']:
                self.link_exists = True
                output, code = [], 0
            elif argv[0] == 'sudo':
                output, code = [], 0
            elif 'addr' in argv:
                output, code = [{'addr_info':[{'local':'172.17.0.1','prefixlen':16}]}], 0
            else:
                output, code = [{'flags':['UP'],'linkinfo':{'info_kind':'bridge'}}], 0 if self.link_exists else 1
            return subprocess.CompletedProcess(argv, code, json.dumps(output), '')
        return run

    def test_missing_bridge_blocks_without_mutation(self):
        with self.assertRaisesRegex(ValueError, 'missing'):
            bridge_preflight({}, runner=self.runner())
        self.assertFalse(any(argv[0]=='sudo' for argv in self.commands))

    def test_repair_only_missing_link_without_daemon_or_firewall_changes(self):
        result = bridge_preflight({}, repair=True, runner=self.runner())
        self.assertTrue(result['repaired_missing_link'])
        self.assertTrue(all(argv[2]=='ip' for argv in self.commands if argv[0]=='sudo'))

    def test_attached_containers_prevent_repair(self):
        with self.assertRaisesRegex(ValueError, 'zero attached'):
            bridge_preflight({}, repair=True, runner=self.runner(attached=True))
        self.assertFalse(any(argv[0]=='sudo' for argv in self.commands))

    def test_shared_link_repair_keeps_forwarding_read_only(self):
        with patch('tools.research_handoff.core.docker_network.model_forwarding',
                   return_value={'checked': True}) as forwarding:
            bridge_preflight({'enabled': True, 'bridge_interface': 'docker0'},
                             repair=True, runner=self.runner())
        self.assertFalse(forwarding.call_args.kwargs['repair'])
        self.assertFalse(forwarding.call_args.kwargs['restore_default_forwarding'])

    def test_policy_rejects_public_runtime_and_host_build(self):
        for invalid in ({'build_network':'host'}, {'runtime_mode':'public'}, {'model_host_addresses':{'api.invalid':'127.0.0.1; bad'}}):
            with self.assertRaises(ValueError):
                validate_network_config(invalid)
        rules = egress_rules(['14.103.169.114', '2001:db8::1'])
        self.assertIn('policy drop', rules)
        self.assertIn('tcp dport 443 accept', rules)
        self.assertNotIn('udp dport 53 accept', rules)


class ForwardingTests(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.present = set()
        self.forward_order = []
        self.config = validate_network_config({"enabled": True, "bridge_interface": "research0",
                                              "model_host_addresses": {"model.example": "203.0.113.8"}})
        self.nat = ("nat", "POSTROUTING", ("-s", "10.10.0.0/24", "!", "-o", "research0", "-j", "MASQUERADE"))
        self.present.add(self.nat)

    def runner(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[0] == "sysctl":
            return subprocess.CompletedProcess(argv, 0, "1\n", "")
        table, action, chain = argv[6:9]
        rule = tuple(argv[9:])
        key = (table, chain, rule)
        if action == '-S':
            return subprocess.CompletedProcess(argv, 0,
                '\n'.join('-A FORWARD ' + __import__('shlex').join(row) for row in self.forward_order), '')
        if action == "-C":
            code = 0 if key in self.present else 1
        elif action == '-D':
            self.present.remove(key)
            self.forward_order.remove(list(rule))
            code = 0
        else:
            self.present.add((table, chain, rule[1:] if action == "-I" else rule))
            if table == 'filter' and chain == 'FORWARD':
                self.forward_order.append(list(rule))
            code = 0
        return subprocess.CompletedProcess(argv, code, "", "")

    def test_missing_forwarding_is_detected_without_mutation(self):
        with self.assertRaisesRegex(ValueError, "HTTPS forwarding"):
            model_forwarding(self.config, "research0", "10.10.0.0/24", runner=self.runner)
        self.assertFalse(any("-I" in args or "-A" in args for args in self.commands))

    def test_repair_is_scoped_to_endpoint_and_bridge_and_is_idempotent(self):
        self.config["repair_forwarding"] = True
        result = model_forwarding(self.config, "research0", "10.10.0.0/24", repair=True, runner=self.runner)
        self.assertEqual(len(result["repaired_rules"]), 2)
        for row in result["repaired_rules"]:
            rule = row["rule"]
            self.assertIn("research0", rule)
            self.assertIn("203.0.113.8", rule)
            self.assertIn("443", rule)
            self.assertIn("10.10.0.0/24", rule)
        again = model_forwarding(self.config, "research0", "10.10.0.0/24", repair=True, runner=self.runner)
        self.assertEqual(again["repaired_rules"], [])
        self.assertFalse(any("-F" in args or "-P" in args or "systemctl" in args for args in self.commands))

    def test_wrong_bridge_or_public_bridge_is_never_repaired(self):
        for name in ("other0", "docker0"):
            with self.assertRaisesRegex(ValueError, "exact dedicated"):
                model_forwarding(self.config, name, "10.10.0.0/24", repair=True, runner=self.runner)
        self.assertFalse(any("-I" in args or "-A" in args for args in self.commands))

    def test_healthy_native_docker_forwarding_needs_no_extra_rules(self):
        self.present.update({("filter", "FORWARD", ("-j", "DOCKER-FORWARD")),
                             ("filter", "DOCKER-FORWARD", ("-i", "research0", "-j", "ACCEPT")),
                             ("filter", "DOCKER-CT", ("-o", "research0", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"))})
        result = model_forwarding(self.config, "research0", "10.10.0.0/24", runner=self.runner)
        self.assertTrue(result["native_docker_forwarding"])
        self.assertEqual(result["repaired_rules"], [])

    def test_explicit_default_restoration_allows_build_egress_and_is_idempotent(self):
        self.config['bridge_interface'] = 'docker0'
        result = model_forwarding(self.config, 'docker0', '172.17.0.0/16',
                                  restore_default_forwarding=True, runner=self.runner)
        self.assertTrue(result['restored_default_forwarding'])
        self.assertEqual(len(result['repaired_rules']), 3)  # NAT and two forwarding rules.
        for row in result['repaired_rules']:
            self.assertIn('docker0', row['rule'])
            self.assertIn('172.17.0.0/16', row['rule'])
        self.assertNotIn('--dport', result['repaired_rules'][1]['rule'])
        self.assertIn('RELATED,ESTABLISHED', result['repaired_rules'][2]['rule'])
        again = model_forwarding(self.config, 'docker0', '172.17.0.0/16',
                                 restore_default_forwarding=True, runner=self.runner)
        self.assertEqual(again['repaired_rules'], [])
        checked = model_forwarding(self.config, 'docker0', '172.17.0.0/16', runner=self.runner)
        self.assertTrue(checked['restored_default_forwarding'])
        self.assertFalse(any('-F' in cmd or '-P' in cmd or 'systemctl' in cmd for cmd in self.commands))

    def test_default_restoration_preserves_user_chain_and_repairs_old_rule_order(self):
        self.config['bridge_interface'] = 'docker0'
        model_forwarding(self.config, 'docker0', '172.17.0.0/16',
                         restore_default_forwarding=True, runner=self.runner)
        self.forward_order.append(['-j', 'DOCKER-USER'])
        self.commands.clear()
        result = model_forwarding(self.config, 'docker0', '172.17.0.0/16',
                                  restore_default_forwarding=True, runner=self.runner)
        self.assertEqual(len(result['relocated_default_rules']), 2)
        self.assertEqual(self.forward_order[0], ['-j', 'DOCKER-USER'])
        self.assertFalse(any('-I' in command for command in self.commands))
        again = model_forwarding(self.config, 'docker0', '172.17.0.0/16',
                                 restore_default_forwarding=True, runner=self.runner)
        self.assertEqual(again['repaired_rules'], [])
        self.assertEqual(again['relocated_default_rules'], [])

    def test_rule_order_recognizes_iptables_canonical_option_order(self):
        self.config['bridge_interface'] = 'docker0'
        model_forwarding(self.config, 'docker0', '172.17.0.0/16',
                         restore_default_forwarding=True, runner=self.runner)
        self.forward_order.append(['-j', 'DOCKER-USER'])
        original = self.runner
        def canonical(argv, **kwargs):
            result = original(argv, **kwargs)
            if '-S' in argv:
                result.stdout = result.stdout.replace('-i docker0 -s 172.17.0.0/16', '-s 172.17.0.0/16 -i docker0')
                result.stdout = result.stdout.replace('-o docker0 -d 172.17.0.0/16', '-d 172.17.0.0/16 -o docker0')
            return result
        result = model_forwarding(self.config, 'docker0', '172.17.0.0/16',
                                  restore_default_forwarding=True, runner=canonical)
        self.assertEqual(len(result['relocated_default_rules']), 2)
        self.assertEqual(self.forward_order[0], ['-j', 'DOCKER-USER'])

    def test_shared_default_recovery_requires_exact_explicit_bridge(self):
        with self.assertRaisesRegex(ValueError, 'explicit docker0'):
            model_forwarding(self.config, 'docker0', '172.17.0.0/16',
                             restore_default_forwarding=True, runner=self.runner)
        self.assertFalse(any('-I' in cmd or '-A' in cmd for cmd in self.commands))


class DedicatedBuildForwardingTests(unittest.TestCase):
    def setUp(self):
        self.commands, self.present = [], set()
        self.config = {'enabled': True, 'repair_forwarding': True,
                       'bridge_interface': 'research0',
                       'docker_host': 'unix:///mnt/data/docker.sock'}
        self.network = {'Driver': 'bridge', 'Id': 'dedicated-id',
                        'Options': {'com.docker.network.bridge.name': 'research0'},
                        'IPAM': {'Config': [{'Subnet': '10.10.0.0/24'}]}}

    def runner(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[0] == 'docker':
            output, code = json.dumps([self.network]), 0
        elif argv[0] == 'sysctl':
            output, code = '1\n', 0
        elif argv[0] == 'ip':
            output, code = json.dumps([{'flags': ['UP'], 'linkinfo': {'info_kind': 'bridge'}}]), 0
        else:
            table, action, chain = argv[6:9]
            key = (table, chain, tuple(argv[9:]))
            if action == '-C':
                code = 0 if key in self.present else 1
            else:
                self.assertEqual(action, '-A')
                self.present.add(key)
                code = 0
            output = ''
        return subprocess.CompletedProcess(argv, code, output, '')

    def test_build_access_is_scoped_and_idempotent(self):
        result = dedicated_build_forwarding(self.config, runner=self.runner)
        self.assertEqual(len(result['added_rules']), 4)
        self.assertEqual(result['allowed_build_ports'], [53, 80, 443])
        for row in result['added_rules']:
            self.assertIn('research0', row['rule'])
            self.assertIn('10.10.0.0/24', row['rule'])
        self.assertIn('--dports', result['added_rules'][1]['rule'])
        self.assertIn('53,80,443', result['added_rules'][2]['rule'])
        self.assertIn('RELATED,ESTABLISHED', result['added_rules'][3]['rule'])
        again = dedicated_build_forwarding(self.config, runner=self.runner)
        self.assertEqual(again['added_rules'], [])
        self.assertFalse(any('-I' in cmd or '-F' in cmd or '-P' in cmd or 'systemctl' in cmd
                             for cmd in self.commands))

    def test_explicit_scope_is_required_before_mutation(self):
        for changes in ({'enabled': False}, {'repair_forwarding': False},
                        {'bridge_interface': 'docker0'}, {'bridge_interface': 'other0'}):
            self.commands.clear()
            with self.assertRaises(ValueError):
                dedicated_build_forwarding({**self.config, **changes}, runner=self.runner)
            self.assertFalse(any(cmd[0] == 'sudo' for cmd in self.commands))

    def test_forwarding_disabled_is_reported_without_sysctl_change(self):
        original = self.runner
        def disabled(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[0] == 'sysctl':
                result.stdout = '0\n'
            return result
        with self.assertRaisesRegex(ValueError, 'disabled'):
            dedicated_build_forwarding(self.config, runner=disabled)
        self.assertFalse(any(cmd[0] == 'sudo' for cmd in self.commands))


if __name__ == '__main__':
    unittest.main()
