"""Consent-based, per-user launchers that follow the running AppImage."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from PySide6 import QtCore, QtWidgets as W
from paths import data_directory
from usb_setup import system_environment

ROOT = Path(__file__).resolve().parent
ENTRY_NAME = 'hantek-linux.desktop'
MANAGED = 'X-Hantek-Managed=true'


def appimage_path():
    
    value = os.environ.get('APPIMAGE', '')
    path = Path(value)
    if getattr(sys, 'frozen', False) and value and path.is_absolute() and path.is_file():
        return path.resolve()
    return None


def clean_environment():
    environment = system_environment()
    return {key: environment.value(key) for key in environment.keys()}


def xdg_base(variable, fallback):
    value = Path(os.environ.get(variable, ''))
    return value if value.is_absolute() else Path.home() / fallback


def desktop_directory():
    program = shutil.which('xdg-user-dir')
    if program:
        result = subprocess.run([program, 'DESKTOP'], capture_output=True, text=True,
                                env=clean_environment(), timeout=5)
        value = Path(result.stdout.strip())
        if result.returncode == 0 and value.is_absolute():
            
            return None if value == Path.home() else value
    return Path.home() / 'Desktop'


def destinations():
    desktop = desktop_directory()
    return {'desktop': desktop / ENTRY_NAME if desktop else None,
            'menu': xdg_base('XDG_DATA_HOME', '.local/share') / 'applications' / ENTRY_NAME}


def preferences_path():
    return data_directory() / 'shortcuts.json'


def read_preferences():
    try:
        data = json.loads(preferences_path().read_text())
        if data.get('version') == 1 and all(type(data.get(key)) is bool for key in ('desktop', 'menu')):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return None


def atomic_write(path, content, mode):
    """Avoid touching an unchanged launcher, including its desktop trust metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f'Refusing to replace a symbolic link: {path}')
    try:
        if path.read_bytes() == content:
            if path.stat().st_mode & 0o777 != mode:
                path.chmod(mode)
            return False
    except FileNotFoundError:
        pass
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.hantek-', delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            os.fchmod(file.fileno(), mode)
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return True


def save_preferences(preferences):
    atomic_write(preferences_path(), (json.dumps(preferences, indent=2) + '\n').encode(), 0o600)


def entry_value(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')


def exec_argument(value):
    
    quoted = ''.join('\\' + char if char in '\\"`$' else char for char in str(value))
    return '"' + entry_value(quoted).replace('%', '%%') + '"'


def entry_text(image, icon):
    executable = exec_argument(image)
    if any(char in str(image) for char in '=%'):
        
        
        
        executable = '/bin/sh -c ' + exec_argument('exec "$1"') + ' hantek ' + executable
    return ('[Desktop Entry]\nType=Application\nName=TraceForge\n'
            'Comment=Linux oscilloscope for the Hantek 1008 series\n'
            f'Exec={executable}\nIcon={entry_value(icon)}\n'
            'Terminal=false\nCategories=Science;Electronics;\nStartupNotify=true\n'
            f'StartupWMClass=hantek-linux\n{MANAGED}\n')


def owned_entry(path):
    if path.is_symlink():
        return False
    try:
        return MANAGED in path.read_text().splitlines()
    except FileNotFoundError:
        return False


def refresh_desktop_database(directory):
    program = shutil.which('update-desktop-database')
    if program:
        try:
            subprocess.run([program, str(directory)], capture_output=True,
                           env=clean_environment(), timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass  


def sync_shortcuts(image, preferences, targets=None, explicit_consent=False):
    """Only manage approved targets. Existing unrelated entries are preserved."""
    targets = destinations() if targets is None else targets
    if not image.is_file():
        raise FileNotFoundError(f'The running AppImage could not be found: {image}')
    icon = data_directory() / 'icon.svg'
    if preferences['desktop'] or preferences['menu']:
        atomic_write(icon, (ROOT / 'icon.svg').read_bytes(), 0o644)
    content = entry_text(image, icon).encode()
    changed = []
    errors = []
    for key in ('desktop', 'menu'):
        target = targets[key]
        if target is None:
            if preferences[key]:
                errors.append('This account has no configured Desktop folder.')
            continue
        try:
            if preferences[key]:
                if target.exists() or target.is_symlink():
                    if not owned_entry(target):
                        
                        old = target.read_text() if not target.is_symlink() else ''
                        if not explicit_consent or not {'Name=Hantek Linux Scope', 'Name=TraceForge'}.intersection(old.splitlines()):
                            raise OSError(f'A different shortcut already exists at {target}; it was preserved.')
                        backup = data_directory() / 'shortcut-backups' / f'{key}-{time.time_ns()}.desktop'
                        atomic_write(backup, old.encode(), 0o600)
                if atomic_write(target, content, 0o755):
                    changed.append(key)
            elif explicit_consent and owned_entry(target):
                target.unlink()
                changed.append(key)
        except OSError as exc:
            errors.append(str(exc))
    if 'menu' in changed:
        refresh_desktop_database(targets['menu'].parent)
    if errors:
        raise OSError('\n'.join(errors))
    return changed


class ShortcutDialog(W.QDialog):
    def __init__(self, image, preferences=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Shortcuts · TraceForge')
        self.setMinimumWidth(580)
        self.targets = destinations()
        layout = W.QVBoxLayout(self)
        title = W.QLabel('Add app shortcuts?')
        title.setStyleSheet('font-size: 20px; font-weight: 700')
        layout.addWidget(title)
        text = W.QLabel('Choose where you want TraceForge to appear. No administrator password is needed.\n\n'
                       'Your choices are remembered. Each launch checks the approved shortcuts and updates them '
                       'to this AppImage’s current folder and filename.\n\n'
                       'After moving or renaming the AppImage, open it from its new location once to update the shortcuts.')
        text.setWordWrap(True)
        layout.addWidget(text)
        current = W.QLabel(f'Current AppImage: {image}')
        current.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        current.setWordWrap(True)
        current.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(current)
        selected = preferences or {'desktop': True, 'menu': True}
        self.checks = {}
        for key, label in (('desktop', 'Desktop shortcut'), ('menu', 'Application menu / Start menu')):
            check = W.QCheckBox(label)
            check.setChecked(selected[key] and self.targets[key] is not None)
            check.setEnabled(self.targets[key] is not None)
            check.setToolTip(str(self.targets[key]) if self.targets[key] else 'No Desktop folder is configured')
            layout.addWidget(check)
            self.checks[key] = check
        note = W.QLabel('You can change these choices later with the Shortcuts button in the app.' +
                        (' Unchecking an option removes that app-managed shortcut.' if preferences else ' Skip creates no shortcuts and is remembered.'))
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = W.QDialogButtonBox()
        self.skip_button = buttons.addButton('Cancel' if preferences else 'Skip', W.QDialogButtonBox.ButtonRole.RejectRole)
        self.accept_button = buttons.addButton('Save choices' if preferences else 'Create shortcuts', W.QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choices(self):
        return {'version': 1, **{key: checkbox.isChecked() for key, checkbox in self.checks.items()}}


def ensure_shortcuts(parent=None, force=False):
    image = appimage_path()
    if image is None:
        if force:
            W.QMessageBox.information(parent, 'AppImage shortcuts', 'Open the AppImage to create or update its shortcuts.')
        return
    try:
        preferences = read_preferences()
        explicit_consent = False
        targets = None
        if preferences is None or force:
            dialog = ShortcutDialog(image, preferences, parent)
            if dialog.exec() == W.QDialog.DialogCode.Accepted:
                preferences = dialog.choices()
                targets = dialog.targets
                explicit_consent = True
            elif preferences is None:
                preferences = {'version': 1, 'desktop': False, 'menu': False}
            else:
                return
            save_preferences(preferences)
        if preferences['desktop'] or preferences['menu'] or explicit_consent:
            sync_shortcuts(image, preferences, targets, explicit_consent)
    except (OSError, subprocess.TimeoutExpired) as exc:
        W.QMessageBox.warning(parent, 'Shortcut setup', f'Shortcuts could not be fully updated:\n\n{exc}\n\n'
                              'The scope app can still open. Use its Shortcuts button to retry.')
