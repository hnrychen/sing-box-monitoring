import unittest
from unittest.mock import patch
import monitorctl


class ManagementTests(unittest.TestCase):
    def test_scope_never_includes_proxy_or_other_infrastructure(self):
        self.assertEqual(set(monitorctl.selected('all')), {'exporter', 'ipinfo', 'connections', 'probes'})
        units = {spec[0] for spec in monitorctl.COMPONENTS.values()}
        self.assertNotIn('sing-box.service', units)
        self.assertNotIn('prometheus.service', units)
        self.assertNotIn('grafana.service', units)

    def test_unmanaged_service_and_absent_component_guards(self):
        with patch.object(monitorctl, 'run', return_value='not-found'):
            self.assertFalse(monitorctl.installed('probes'))
        with patch.object(monitorctl, 'run', side_effect=['loaded', '/unrelated/application']):
            with self.assertRaises(RuntimeError):
                monitorctl.installed('probes')
        with patch.object(monitorctl, 'run', side_effect=['loaded', '/opt/sing-box-probe/proxy_probe.py']):
            self.assertTrue(monitorctl.installed('probes'))
