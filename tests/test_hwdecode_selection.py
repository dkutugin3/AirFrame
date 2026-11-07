"""Portable decoder selection contracts; no hardware device is required."""
import unittest
from unittest.mock import patch

from airframe import hwdecode as hw


class PlatformSelectionTests(unittest.TestCase):
    def test_windows_hardware_opt_in_probes_d3d11va(self):
        probed = []

        def unavailable(device):
            probed.append(device)
            return hw.HwProbe(device, False, False, 'no device')

        with patch.object(hw.sys, 'platform', 'win32'):
            factory = hw.select_decoder_factory('hardware', probe=unavailable)
        self.assertEqual(probed, ['d3d11va'])
        self.assertEqual(factory.status['selectedMode'], 'software')
        self.assertIn('no device', factory.status['reason'])

    def test_explicit_device_is_preserved_on_windows(self):
        probed = []

        def unavailable(device):
            probed.append(device)
            return hw.HwProbe(device, False, False, 'no device')

        with patch.object(hw.sys, 'platform', 'win32'):
            hw.select_decoder_factory('hardware', device_type='cuda', probe=unavailable)
        self.assertEqual(probed, ['cuda'])

    def test_windows_auto_does_not_apply_a_macos_performance_measurement(self):
        def verified(device):
            return hw.HwProbe(device, True, True, '')

        with patch.object(hw.sys, 'platform', 'win32'):
            factory = hw.select_decoder_factory('auto', probe=verified)
        self.assertEqual(factory.status['selectedMode'], 'software')
        self.assertNotIn('measured slower', factory.status['reason'])
        self.assertIn('not benchmarked', factory.status['reason'])
