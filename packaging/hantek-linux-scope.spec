from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH).parent
assets = [(str(root/name), '.') for name in ('icon.svg', 'install-usb-access.sh', 'usb-setup-terminal.sh', 'README.md', 'TIMING.md')]
assets += [(str(root/'references'), 'references'), (str(root/'vendor/LICENSE'), 'licenses/hantek1008py')]
assets += [(str(root/'.native/ffmpeg-license.txt'), 'licenses/ffmpeg')]
assets += [(str(root/'.native/COPYING.GPLv3'), 'licenses/ffmpeg')]
assets += [(str(root/'packaging/fonts.conf'), '.')]
for package in ('PySide6', 'PySide6_Essentials', 'PySide6_Addons', 'shiboken6', 'numpy', 'pyqtgraph', 'pyusb', 'overrides', 'colorama', 'libusb-package', 'imageio-ffmpeg'):
    assets += copy_metadata(package)
a = Analysis([str(root/'app.py')], pathex=[str(root)],
    binaries=[(str(root/'.native/libusb-1.0.so.0'), '.'), (str(root/'.native/ffmpeg'), 'native')], datas=assets,
    hiddenimports=['usb.backend.libusb1', 'PySide6.QtSvg'],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'tkinter', 'matplotlib', 'scipy', 'pandas', 'IPython', 'OpenGL'],
    noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='hantek-linux-scope',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='hantek-linux-scope')
