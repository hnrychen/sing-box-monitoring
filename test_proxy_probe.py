import copy
import json
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import proxy_probe
from integrate_probe_prometheus import update


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = json.loads(pathlib.Path(__file__).with_name('probe.example.json').read_text())
        self.config['state_file'] = self.directory.name + '/state.json'
        self.config['runtime_dir'] = self.directory.name

    def test_loopback_bounded_and_private_config_validation(self):
        proxy_probe.validate(self.config)
        for field, bad in [('listen', '0.0.0.0:9122'), ('interval', 1), ('workers', 10), ('timeout', 100)]:
            config = dict(self.config, **{field: bad})
            with self.assertRaises(ValueError):
                proxy_probe.validate(config)
        config = copy.deepcopy(self.config)
        config['checks'][0]['url'] = 'https://user:secret@example.org'
        with self.assertRaises(ValueError):
            proxy_probe.validate(config)

    def test_real_outbound_and_isolated_loopback_routing(self):
        result = proxy_probe.client_config(self.config)
        self.assertEqual(result['inbounds'][0]['listen'], '127.0.0.1')
        self.assertEqual(result['outbounds'][0]['password'], 'REPLACE_WITH_PRIVATE_CREDENTIAL')
        self.assertEqual(result['route']['rules'][0]['outbound'], result['outbounds'][0]['tag'])
        self.assertNotIn('experimental', result)

    def test_curl_forces_proxy_no_redirect_and_checks_response_body(self):
        def curl(command, **kwargs):
            self.assertIn('socks5h://127.0.0.1:19360', command)
            self.assertEqual(command[command.index('--noproxy') + 1], '')
            self.assertEqual(command[command.index('--max-redirs') + 1], '0')
            self.assertEqual(command[0:2], ['/usr/bin/curl', '-q'])
            pathlib.Path(command[command.index('--output') + 1]).write_text('ip=203.0.113.10\n')
            return subprocess.CompletedProcess(command, 0, json.dumps(dict(http_code=200, time_total=0.125)), '')
        with patch.object(proxy_probe.subprocess, 'run', side_effect=curl):
            result = proxy_probe.run_check(self.config, 0, self.config['checks'][0], self.directory.name)
        self.assertEqual(result['success'], 1)
        self.assertEqual(result['duration'], 0.125)

    def test_failures_and_challenges_never_keep_success_latency(self):
        prober = proxy_probe.Prober(self.config)
        target, check = self.config['targets'][0], self.config['checks'][0]
        prober.record(target, check, dict(success=1, status=200, duration=0.1, attempt_duration=0.1))
        self.assertIn('singbox_probe_duration_seconds{', prober.render().decode())
        failed = subprocess.CompletedProcess([], 0, json.dumps(dict(http_code=200, time_total=0.2)), 'private stderr')
        with patch.object(proxy_probe.subprocess, 'run', return_value=failed):
            result = proxy_probe.run_check(self.config, 0, check, self.directory.name)
        self.assertEqual(result['success'], 0)  # Missing/invalid trace body, even HTTP 200.
        prober.record(target, check, result)
        text = prober.render().decode()
        self.assertNotIn('singbox_probe_duration_seconds{', text)
        self.assertNotIn('private stderr', text)
        self.assertNotIn('REPLACE_WITH_PRIVATE_CREDENTIAL', text)
        self.assertEqual(prober.counts[(target['id'], check['name'], 'failure')], 1)

    def test_freshness_pending_and_persistent_outcomes(self):
        prober = proxy_probe.Prober(self.config)
        target, check = self.config['targets'][0], self.config['checks'][0]
        self.assertNotIn('singbox_probe_success{', prober.render().decode())
        prober.record(target, check, dict(success=1, status=200, duration=0.1, attempt_duration=0.1))
        prober.save()
        restored = proxy_probe.Prober(self.config)
        self.assertEqual(restored.counts[(target['id'], check['name'], 'success')], 1)
        self.assertNotIn('singbox_probe_success{', restored.render().decode())  # no stale snapshot restored.
        prober.results[(target['id'], check['name'])]['timestamp'] = time.time() - 1000
        self.assertNotIn('singbox_probe_success{', prober.render().decode())
        prober.runner_up = 1
        prober.last_cycle = time.time() - 1000
        self.assertIn('singbox_probe_runner_up{} 0', prober.render().decode())

    def test_prometheus_merge_preserves_jobs_and_is_idempotent(self):
        before = 'global:\n  scrape_interval: 15s\nscrape_configs:\n  - job_name: existing\n    static_configs: []\nrule_files:\n  - /rules.yml\n'
        after = update(before)
        self.assertIn('job_name: existing', after)
        self.assertIn('honor_labels: true', after)
        self.assertLess(after.index('job_name: sing-box-probes'), after.index('rule_files:'))
        self.assertEqual(update(after), after)
        with self.assertRaises(ValueError):
            update(before.replace('job_name: existing', 'job_name: sing-box-probes'))
        reordered = 'rule_files:\n  - /rules.yml\n' + before.split('rule_files:')[0] + 'remote_write:\n  - url: https://example.org\n'
        result = update(reordered)
        self.assertLess(result.index('job_name: existing'), result.index('job_name: sing-box-probes'))
        self.assertLess(result.index('job_name: sing-box-probes'), result.index('remote_write:'))
        self.assertEqual(update(result), result)
        with self.assertRaises(ValueError):
            update('global:\n  scrape_interval: 15s\n')


if __name__ == '__main__':
    unittest.main()
