"""Read-only USB identification, independent of permission setup and capture."""
from dataclasses import dataclass
import errno

from PySide6 import QtCore, QtWidgets as W
import usb.backend.libusb1
import usb.core
import usb.util

VENDOR_ID, PRODUCT_ID = 0x0783, 0x5725


@dataclass(frozen=True)
class Detection:
    state: str
    title: str
    detail: str
    count: int = 0

    @property
    def ready(self):
        return self.state == 'ready'


def detect_device():
    """Read a standard descriptor; never configure, claim, reset or initialize."""
    device = None
    try:
        backend = usb.backend.libusb1.get_backend()
        if backend is None:
            return Detection('backend_missing', 'USB support unavailable',
                             'The libusb backend could not load. Reopen the complete AppImage or check the source installation.')
        devices = list(usb.core.find(find_all=True, idVendor=VENDOR_ID, idProduct=PRODUCT_ID, backend=backend))
        if not devices:
            return Detection('missing', 'Hantek not connected',
                             'Plug in the scope, then choose Detect device or Connect + run. Saved recordings are still available.')
        if len(devices) > 1:
            return Detection('multiple', f'{len(devices)} Hantek devices detected',
                             'Leave one scope connected, then choose Detect device. This app captures from one scope at a time.', len(devices))
        device = devices[0]
        
        
        descriptor = bytes(device.ctrl_transfer(0x80, 0x06, 0x0100, 0, 18, timeout=500))
        if (len(descriptor) != 18 or descriptor[1] != 1 or
                int.from_bytes(descriptor[8:10], 'little') != VENDOR_ID or
                int.from_bytes(descriptor[10:12], 'little') != PRODUCT_ID):
            return Detection('error', 'Hantek USB check failed',
                             'The device returned an unexpected USB descriptor. Reconnect it and choose Detect device.', 1)
        location = f'USB bus {device.bus}, device {device.address}'
        return Detection('ready', 'Hantek detected · USB access confirmed',
                         f'{location} · 0783:5725. Connect + run to begin acquisition.', 1)
    except usb.core.USBError as exc:
        number, backend_code = getattr(exc, 'errno', None), getattr(exc, 'backend_error_code', None)
        if number in (errno.EACCES, errno.EPERM) or backend_code == -3:
            return Detection('permission', 'Hantek detected · USB permission needed',
                             'Complete USB setup, or reconnect the scope if setup is already installed.', 1)
        if number == errno.ENODEV or backend_code == -4:
            return Detection('missing', 'Hantek disconnected during detection',
                             'Reconnect the scope and choose Detect device.')
        if number == errno.EBUSY or backend_code == -6:
            return Detection('busy', 'Hantek detected · USB is busy',
                             'Close any other scope software, then choose Detect device.', 1)
        return Detection('error', 'Hantek USB check failed',
                         f'{exc}. Check the USB connection and choose Detect device.', 1 if device is not None else 0)
    except Exception as exc:
        return Detection('error', 'USB detection could not finish', str(exc))
    finally:
        if device is not None:
            try:
                usb.util.dispose_resources(device)
            except usb.core.USBError:
                pass


class DetectionWorker(QtCore.QThread):
    def run(self):
        self.result = detect_device()


class DetectionDialog(W.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('TraceForge · Detecting Hantek')
        self.setMinimumWidth(430)
        layout = W.QVBoxLayout(self)
        layout.addWidget(W.QLabel('Checking for the Hantek and testing USB access…'))
        progress = W.QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(progress)
        self.worker = DetectionWorker(self)
        self.worker.finished.connect(self.accept)
        QtCore.QTimer.singleShot(0, self.worker.start)

    def reject(self):
        
        if not self.worker.isRunning():
            super().reject()


def scan_device(parent=None):
    dialog = DetectionDialog(parent)
    dialog.exec()
    dialog.worker.wait()
    return getattr(dialog.worker, 'result', Detection('error', 'USB check cancelled', 'Choose Detect device to retry.'))
