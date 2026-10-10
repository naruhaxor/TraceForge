import ctypes
import os
import shlex
import shutil
import subprocess
import sys

from builder.ui import Cancelled

LIBRARIES = {
    'libGL.so.1': 'libgl1', 'libEGL.so.1': 'libegl1',
    'libxkbcommon.so.0': 'libxkbcommon0', 'libxkbcommon-x11.so.0': 'libxkbcommon-x11-0',
    'libxcb-cursor.so.0': 'libxcb-cursor0', 'libxcb-icccm.so.4': 'libxcb-icccm4',
    'libxcb-image.so.0': 'libxcb-image0', 'libxcb-keysyms.so.1': 'libxcb-keysyms1',
    'libxcb-render-util.so.0': 'libxcb-render-util0', 'libxcb-randr.so.0': 'libxcb-randr0',
    'libxcb-xinerama.so.0': 'libxcb-xinerama0', 'libxcb-xfixes.so.0': 'libxcb-xfixes0',
    'libxcb-shape.so.0': 'libxcb-shape0', 'libxcb-sync.so.1': 'libxcb-sync1',
    'libxcb-xkb.so.1': 'libxcb-xkb1', 'libdbus-1.so.3': 'libdbus-1-3',
    'libfontconfig.so.1': 'libfontconfig1', 'libfreetype.so.6': 'libfreetype6',
    'libglib-2.0.so.0': 'libglib2.0-0', 'libX11.so.6': 'libx11-6',
    'libXext.so.6': 'libxext6', 'libXrender.so.1': 'libxrender1',
    'libSM.so.6': 'libsm6', 'libICE.so.6': 'libice6',
}


def missing_requirements(work):
    missing = [name for name in ('cc', 'make', 'cmake', 'file', 'readelf') if not shutil.which(name)]
    if shutil.which('cc'):
        test = work / 'zlib-check'
        result = subprocess.run(['cc', '-x', 'c', '-', '-o', str(test), '-lz'],
                                input='#include <zlib.h>\nint main(void) { return zlibVersion() == 0; }\n',
                                capture_output=True, text=True)
        test.unlink(missing_ok=True)
        if result.returncode:
            missing.append('zlib development files')
    for library in LIBRARIES:
        try:
            ctypes.CDLL(library)
        except OSError:
            missing.append(library)
    return missing


def install_commands(bubblewrap=False):
    if shutil.which('apt-get'):
        packages = ['bubblewrap'] if bubblewrap else ['build-essential', 'cmake', 'file', 'binutils', 'zlib1g-dev', *LIBRARIES.values()]
        return [['sudo', 'apt-get', 'update'], ['sudo', 'apt-get', 'install', '-y', *packages]]
    if shutil.which('dnf'):
        packages = ['bubblewrap'] if bubblewrap else ['gcc', 'make', 'cmake', 'file', 'binutils', 'zlib-devel',
                    *[name+'()(64bit)' for name in LIBRARIES]]
        return [['sudo', 'dnf', 'install', '-y', *packages]]
    if shutil.which('pacman'):
        packages = ['bubblewrap'] if bubblewrap else ['base-devel', 'cmake', 'file', 'binutils', 'zlib', 'libglvnd',
                    'libxkbcommon', 'libxkbcommon-x11', 'xcb-util-cursor', 'xcb-util-wm', 'xcb-util-image',
                    'xcb-util-keysyms', 'xcb-util-renderutil', 'libxcb', 'dbus', 'fontconfig', 'freetype2',
                    'glib2', 'libx11', 'libxext', 'libxrender', 'libsm', 'libice']
        return [['sudo', 'pacman', '-S', '--needed', '--noconfirm', *packages]]
    if shutil.which('zypper'):
        packages = ['bubblewrap'] if bubblewrap else ['gcc', 'make', 'cmake', 'file', 'binutils', 'zlib-devel',
                    *[name+'()(64bit)' for name in LIBRARIES]]
        return [['sudo', 'zypper', '--non-interactive', 'install', *packages]]
    raise RuntimeError('No supported package manager found. Install the missing prerequisites listed above, then retry.')


def install(prompt, run, approved, bubblewrap=False):
    commands = install_commands(bubblewrap)
    if not approved and not prompt.confirm_install([shlex.join(command) for command in commands]):
        raise Cancelled('System dependency installation cancelled.')
    if not sys.stdin.isatty():
        raise RuntimeError('System packages need sudo in a visible terminal. Run ./build.sh from a terminal.')
    for command in commands:
        run(command)


def ensure_bubblewrap(prompt, run, approved=False):
    if not shutil.which('bwrap'):
        print('Missing portable build prerequisite: bubblewrap', flush=True)
        install(prompt, run, approved, bubblewrap=True)
        if not shutil.which('bwrap'):
            raise RuntimeError('bubblewrap is still missing after installation.')


def ensure_system(work, prompt, run, approved=False):
    if os.geteuid() == 0:
        raise RuntimeError('Run the builder as your normal user. Only missing system packages use sudo.')
    missing = missing_requirements(work)
    if not missing:
        return
    print('Missing build prerequisites: ' + ', '.join(missing), flush=True)
    install(prompt, run, approved)
    remaining = missing_requirements(work)
    if remaining:
        raise RuntimeError('Still missing prerequisites: ' + ', '.join(remaining))
