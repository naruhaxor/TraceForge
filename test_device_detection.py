import errno
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import usb.core
import device_detection as d


class DetectionTests(unittest.TestCase):
    def setUp(self):
        for mocked in (patch.object(d.usb.backend.libusb1, 'get_backend', return_value=object()),
                       patch.object(d.usb.core, 'find'), patch.object(d.usb.util, 'dispose_resources')):
            value = mocked.start()
            self.addCleanup(mocked.stop)
            if mocked.attribute == 'find': self.find = value
            if mocked.attribute == 'dispose_resources': self.dispose = value
        descriptor = bytearray(18)
        descriptor[0:2] = b'\x12\x01'
        descriptor[8:12] = b'\x83\x07\x25\x57'
        self.device = Mock(bus=1, address=7)
        self.device.ctrl_transfer.return_value = descriptor
        self.find.return_value = [self.device]

    def test_read_only_access_probe_releases_handle(self):
        result = d.detect_device()
        self.assertTrue(result.ready)
        self.device.ctrl_transfer.assert_called_once_with(0x80, 0x06, 0x0100, 0, 18, timeout=500)
        self.device.set_configuration.assert_not_called()
        self.device.reset.assert_not_called()
        self.device.write.assert_not_called()
        self.dispose.assert_called_once_with(self.device)

    def test_missing_scope_and_backend_are_distinct(self):
        self.find.return_value = []
        self.assertEqual(d.detect_device().state, 'missing')
        with patch.object(d.usb.backend.libusb1, 'get_backend', return_value=None):
            self.assertEqual(d.detect_device().state, 'backend_missing')

    def test_permissions_disconnect_busy_and_timeout(self):
        for number, state in ((errno.EACCES, 'permission'), (errno.ENODEV, 'missing'),
                              (errno.EBUSY, 'busy'), (errno.ETIMEDOUT, 'error')):
            with self.subTest(number=number):
                self.device.ctrl_transfer.side_effect = usb.core.USBError('test failure', errno=number)
                result = d.detect_device()
                self.assertEqual(result.state, state)
                self.assertFalse(result.ready)
        self.assertEqual(self.dispose.call_count, 4)

    def test_malformed_descriptor_cannot_report_ready(self):
        self.device.ctrl_transfer.return_value = b'\x00' * 18
        self.assertEqual(d.detect_device().state, 'error')

    def test_multiple_scopes_are_not_silently_selected(self):
        self.find.return_value = [self.device, Mock()]
        result = d.detect_device()
        self.assertEqual(result.state, 'multiple')
        self.assertEqual(result.count, 2)
        self.device.ctrl_transfer.assert_not_called()

    def test_each_launch_detects_even_with_saved_setup(self):
        import app
        result = d.Detection('missing', 'No device', 'Plug in the scope')
        with patch.object(app, 'scan_device', return_value=result) as scan, \
             patch.object(app, 'setup_complete', return_value=True), \
             patch.object(app, 'show_usb_setup') as setup, patch.object(app, 'ensure_shortcuts'):
            self.assertIs(app.prepare_startup(), result)
            self.assertIs(app.prepare_startup(), result)
            self.assertEqual(scan.call_count, 2)
            setup.assert_not_called()

    def test_first_launch_detects_before_setup_and_rechecks_after(self):
        import app
        events = []
        denied = d.Detection('permission', 'Needs access', '')
        ready = d.Detection('ready', 'Ready', '', 1)
        results = iter([denied, ready])
        def scan():
            events.append('detect')
            return next(results)
        def setup(**kwargs):
            self.assertEqual(kwargs['detection'], denied)
            events.append('setup')
            return True
        with patch.object(app, 'scan_device', side_effect=scan), \
             patch.object(app, 'setup_complete', return_value=False), \
             patch.object(app, 'show_usb_setup', side_effect=setup), patch.object(app, 'ensure_shortcuts'):
            self.assertIs(app.prepare_startup(), ready)
        self.assertEqual(events, ['detect', 'setup', 'detect'])

    def test_declining_first_setup_still_exits(self):
        import app
        with patch.object(app, 'scan_device', return_value=d.Detection('missing', 'Missing', '')) as scan, \
             patch.object(app, 'setup_complete', return_value=False), \
             patch.object(app, 'show_usb_setup', return_value=False), patch.object(app, 'ensure_shortcuts') as shortcuts:
            self.assertIsNone(app.prepare_startup())
            scan.assert_called_once()
            shortcuts.assert_not_called()

    def test_retry_updates_window_and_missing_does_not_start_capture(self):
        from PySide6 import QtWidgets as W
        import app
        application = W.QApplication.instance() or W.QApplication([])
        application.setQuitOnLastWindowClosed(False)
        window = app.MainWindow()
        try:
            with patch.object(app, 'scan_device', return_value=d.Detection('missing', 'Hantek not connected', 'Plug it in')), \
                 patch.object(app, 'usb_configured', return_value=True), patch.object(app, 'Acquisition') as acquisition:
                window.start_acquisition()
                acquisition.assert_not_called()
                self.assertEqual(window.badge.text(), 'NO DEVICE')
            with patch.object(app, 'scan_device', return_value=d.Detection('ready', 'Hantek detected', 'Access checked', 1)):
                self.assertTrue(window.check_device())
                self.assertEqual(window.badge.text(), 'READY')
        finally:
            window.close()
            application.processEvents()


if __name__ == '__main__':
    unittest.main()
