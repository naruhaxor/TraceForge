"""One-time USB permission setup through a visible sudo terminal."""
from pathlib import Path
import datetime as dt
import json
import os
import shutil
import tempfile

from PySide6 import QtCore, QtWidgets as W
from paths import data_directory

RULE_PATH = Path('/etc/udev/rules.d/70-hantek1008-local.rules')
RULE_TEXT = 'SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0783", ATTR{idProduct}=="5725", MODE="0660", TAG+="uaccess"'
ROOT = Path(__file__).resolve().parent


def rule_installed():
    try:
        return RULE_PATH.read_text().strip() == RULE_TEXT
    except OSError:
        return False


def usb_configured():
    if rule_installed():
        return True
    for device in Path('/sys/bus/usb/devices').glob('*'):
        try:
            if device.joinpath('idVendor').read_text().strip() != '0783' or device.joinpath('idProduct').read_text().strip() != '5725':
                continue
            bus, number = (int(device.joinpath(name).read_text()) for name in ('busnum', 'devnum'))
            if os.access(f'/dev/bus/usb/{bus:03d}/{number:03d}', os.R_OK | os.W_OK):
                return True
        except (OSError, ValueError):
            continue
    return False


def mark_setup_complete():
    if not rule_installed():
        raise RuntimeError('The installed USB access rule could not be verified')
    folder = data_directory()
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / 'usb-setup.json'
    if marker.exists():
        try:
            saved = json.loads(marker.read_text())
            if saved.get('setup_version') == 1 and saved.get('usb_id') == '0783:5725':
                return
        except (OSError, ValueError, AttributeError):
            pass
    
    with tempfile.NamedTemporaryFile(mode='w', dir=folder, prefix='.usb-setup-', delete=False) as file:
        json.dump({'setup_version': 1, 'usb_id': '0783:5725', 'rule_path': str(RULE_PATH),
                   'verified_utc': dt.datetime.now(dt.timezone.utc).isoformat()}, file, indent=2)
        file.write('\n')
        temporary = Path(file.name)
    temporary.replace(marker)


def setup_complete():
    if not rule_installed():
        return False
    
    mark_setup_complete()
    return True


def system_environment():
    env = QtCore.QProcessEnvironment.systemEnvironment()
    original = env.value('LD_LIBRARY_PATH_ORIG')
    if original:
        env.insert('LD_LIBRARY_PATH', original)
    else:
        env.remove('LD_LIBRARY_PATH')
    for name in ('QT_PLUGIN_PATH', 'QML2_IMPORT_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'PYTHONHOME', 'PYTHONPATH'):
        env.remove(name)
    if env.contains('TRACEFORGE_FONTCONFIG_FILE_ORIG'):
        fontconfig = env.value('TRACEFORGE_FONTCONFIG_FILE_ORIG')
        if fontconfig:
            env.insert('FONTCONFIG_FILE', fontconfig)
        else:
            env.remove('FONTCONFIG_FILE')
        env.remove('TRACEFORGE_FONTCONFIG_FILE_ORIG')
    return env


def terminal_command(runner, helper, result):
    title = 'Hantek USB setup - sudo password'
    command = ['/bin/bash', str(runner), str(helper), str(result)]
    candidates = [
        ('konsole', ['--separate', '--workdir', str(runner.parent), '-p', f'tabtitle={title}', '-e']),
        ('ptyxis', ['--standalone', '--title', title, '--']),
        ('gnome-terminal', ['--wait', '--title', title, '--']),
        ('xfce4-terminal', ['--disable-server', '--title', title, '--execute']),
        ('xterm', ['-T', title, '-e']),
        ('kitty', ['--title', title]),
        ('alacritty', ['--title', title, '-e']),
    ]
    for name, arguments in candidates:
        program = shutil.which(name)
        if program:
            return program, arguments + command
    raise RuntimeError('No supported terminal was found. Install Ptyxis, Konsole, GNOME Terminal, Xfce Terminal, xterm, Kitty or Alacritty, then retry.')


class USBSetupDialog(W.QDialog):
    def __init__(self, parent=None, detection=None):
        super().__init__(parent)
        self.setWindowTitle('First-run setup · TraceForge')
        self.setMinimumWidth(540)
        self.process = None
        self.temporary = None
        self.result_path = None
        layout = W.QVBoxLayout(self)
        title = W.QLabel('One-time USB setup')
        title.setStyleSheet('font-size: 20px; font-weight: 700')
        layout.addWidget(title)
        if detection is not None:
            detected = W.QLabel(detection.title + '\n' + detection.detail)
            detected.setWordWrap(True)
            detected.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            layout.addWidget(detected)
        self.explanation = W.QLabel(
            'To work with your Hantek scope, this app needs administrator (sudo) permission to install its USB access rule.\n\n'
            'This only needs to be done once on this computer.\n\n'
            'Accept opens a terminal showing the commands being run and asks for your Linux password. When setup finishes, press any key to close the terminal and open the app.\n\n'
            'Decline exits without making changes.')
        self.explanation.setWordWrap(True)
        layout.addWidget(self.explanation)
        self.status = W.QLabel('')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controls = W.QDialogButtonBox()
        self.decline_button = controls.addButton('Decline', W.QDialogButtonBox.ButtonRole.RejectRole)
        self.accept_button = controls.addButton('Accept', W.QDialogButtonBox.ButtonRole.ActionRole)
        self.decline_button.clicked.connect(self.reject)
        self.accept_button.clicked.connect(self.install_access)
        layout.addWidget(controls)
        if rule_installed():
            self.status.setText('USB access is already installed. Accept will reapply the rule; Decline leaves it unchanged.')

    def install_access(self):
        if self.process and self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            return
        if not shutil.which('sudo'):
            self.status.setText('The sudo command is missing. Install sudo using your Linux system tools, then retry.')
            return
        try:
            self.temporary = tempfile.TemporaryDirectory(prefix='hantek-usb-setup-')
            folder = Path(self.temporary.name)
            
            for name in ('install-usb-access.sh', 'usb-setup-terminal.sh'):
                target = folder / name
                target.write_bytes((ROOT / name).read_bytes())
                target.chmod(0o600)
            self.result_path = folder / 'result'
            program, arguments = terminal_command(folder / 'usb-setup-terminal.sh', folder / 'install-usb-access.sh', self.result_path)
            self.process = QtCore.QProcess(self)
            self.process.setProcessEnvironment(system_environment())
            self.process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
            self.process.finished.connect(self.install_finished)
            self.process.errorOccurred.connect(self.install_error)
            self.accept_button.setEnabled(False)
            self.decline_button.setEnabled(False)
            self.status.setText('Complete setup in the terminal, then press any key there to open the app.')
            self.process.start(program, arguments)
        except Exception as exc:
            self.status.setText(str(exc))
            self.reset_buttons()

    def install_error(self, error):
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self.status.setText('The terminal could not start. USB access was not changed.')
            self.reset_buttons()
            self.show()

    def reset_buttons(self):
        self.accept_button.setEnabled(True)
        self.decline_button.setEnabled(True)
        if self.temporary:
            self.temporary.cleanup()
            self.temporary = None

    def install_finished(self, code, _status):
        result = self.result_path.read_text().strip() if self.result_path and self.result_path.exists() else ''
        output = bytes(self.process.readAllStandardOutput()).decode(errors='replace').strip()
        self.reset_buttons()
        if result == '0' and rule_installed():
            try:
                mark_setup_complete()
            except Exception as exc:
                self.status.setText(f'USB access was installed, but its check file could not be saved: {exc}')
                self.show()
                return
            self.accept()
        else:
            self.status.setText('USB setup was cancelled or did not finish. The app has not started. You can Accept to retry or Decline to exit.' + ('\n' + output[-1000:] if output else ''))
            self.show()

    def reject(self):
        if self.process and self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            return
        super().reject()


def show_usb_setup(parent=None, detection=None):
    dialog = USBSetupDialog(parent, detection)
    accepted = dialog.exec() == W.QDialog.DialogCode.Accepted
    return accepted and setup_complete()
