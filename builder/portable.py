import fcntl
import json
import os
from pathlib import Path
import posixpath
import shutil
import subprocess
import sys
import tarfile

from builder.downloads import fetch, obtain_vendor
from builder.ui import Prompt
from builder.system_deps import ensure_bubblewrap

PACKAGES = ['build-essential', 'cmake', 'zlib1g-dev', 'ca-certificates', 'file', 'desktop-file-utils', 'libgl1', 'libegl1',
            'libxkbcommon0', 'libxkbcommon-x11-0', 'libxcb-cursor0', 'libxcb-icccm4', 'libxcb-image0',
            'libxcb-keysyms1', 'libxcb-render-util0', 'libxcb-randr0', 'libxcb-xinerama0', 'libxcb-xfixes0',
            'libxcb-shape0', 'libxcb-sync1', 'libxcb-xkb1', 'libdbus-1-3', 'libfontconfig1', 'libfreetype6',
            'libglib2.0-0', 'libgtk-3-0', 'libx11-6', 'libxext6', 'libxrender1', 'libsm6', 'libice6', 'libusb-1.0-0', 'fonts-dejavu-core']


def run(command, **kwargs):
    import shlex
    command = [str(value) for value in command]
    print('$ '+shlex.join(command), flush=True)
    subprocess.run(command, check=True, **kwargs)


def extract_rootfs(archive, target):
    def safe(member, destination):
        if member.isdev() or member.isfifo():
            return None
        if member.issym() and member.linkname.startswith('/'):
            member.linkname = posixpath.relpath(member.linkname.lstrip('/'), posixpath.dirname(member.name))
        return tarfile.data_filter(member, destination)
    target.mkdir(parents=True)
    try:
        with tarfile.open(archive) as source:
            source.extractall(target, filter=safe)
    except Exception:
        shutil.rmtree(target)
        raise


def build_portable(args, source):
    if os.geteuid() == 0:
        raise RuntimeError('Run the portable builder as your normal user.')
    ensure_bubblewrap(Prompt(args.no_gui), run, args.install_system_deps)
    base = args.work_dir.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    with (base/'.portable.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sources = json.loads((source/'builder/sources.json').read_text())
        specification = sources['portable']
        prompt = Prompt(args.no_gui)
        vendor = obtain_vendor(sources['vendor'], source, base, prompt, args.vendor_zip, args.download_vendor)
        archive = fetch(specification['rootfs'], base/'downloads')
        root = base/'rootfs'
        if not root.exists():
            extract_rootfs(archive, root)
        resolver = root/'etc/resolv.conf'
        resolver.unlink(missing_ok=True)
        resolver.write_text(Path('/etc/resolv.conf').read_text())
        policy = root/'usr/sbin/policy-rc.d'
        policy.write_text('#!/bin/sh\nexit 101\n')
        policy.chmod(0o755)
        (root/'usr/local/share/fonts').mkdir(parents=True, exist_ok=True)
        overrides = root/'var/lib/dpkg/statoverride'
        launcher = '/usr/lib/dbus-1.0/dbus-daemon-launch-helper'
        entries = overrides.read_text().splitlines() if overrides.exists() else []
        entries = [entry for entry in entries if not entry.endswith(' '+launcher)]
        overrides.write_text('\n'.join(entries+[f'root root 0755 {launcher}'])+'\n')

        def container(command, admin=False):
            binds = []
            for folder in dict.fromkeys((source, base, output)):
                binds += ['--ro-bind' if folder == source else '--bind', str(folder), str(folder)]
            return ['bwrap', '--unshare-user', '--uid', '0' if admin else str(os.getuid()),
                    '--gid', '0' if admin else str(os.getgid()), '--unshare-pid', '--die-with-parent',
                    '--bind', str(root), '/', *binds, '--ro-bind', str(vendor), '/vendor.zip',
                    '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--chdir', str(source),
                    '--setenv', 'PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                    '--setenv', 'DEBIAN_FRONTEND', 'noninteractive', '--setenv', 'APPIMAGE_EXTRACT_AND_RUN', '1',
                    '--setenv', 'TRACEFORGE_MAX_GLIBC', '2.35',
                    '--setenv', 'XDG_CACHE_HOME', str(base/'cache'), '--setenv', 'PIP_CACHE_DIR', str(base/'pip-cache'), *map(str, command)]

        ready = base/'packages-ready.json'
        if not ready.exists() or json.loads(ready.read_text()) != PACKAGES:
            run(container(['apt-get', '-o', 'APT::Sandbox::User=root', 'update'], True))
            run(container(['apt-get', '-o', 'APT::Sandbox::User=root', 'install', '-y', '--no-install-recommends', *PACKAGES], True))
            ready.write_text(json.dumps(PACKAGES))
        python = Path(sys.executable)
        run(container([python, source/'build.py', '--no-gui', '--vendor-zip', '/vendor.zip',
                       '--work-dir', base/'build', '--output-dir', output]))
        print('Portable build completed in isolated Ubuntu 22.04 userspace. Desktop and hardware compatibility still require testing on the target distributions.', flush=True)
