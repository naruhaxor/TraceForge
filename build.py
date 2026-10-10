#!/usr/bin/env python3
import argparse
import ast
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile

from builder.downloads import extract_setup, extract_tar, fetch, obtain_vendor, sha256
from builder.driver_adapter import adapt_driver
from builder.extract_installer import extract
from builder.system_deps import ensure_system
from builder.ui import Cancelled, Prompt
from builder.compatibility import glibc_requirements, version_tuple
from builder.bootstrap import ensure_python, usable_python

ROOT = Path(__file__).resolve().parent


class Builder:
    def __init__(self, args):
        self.args = args
        self.work = args.work_dir.expanduser().resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self.output = args.output_dir.expanduser().resolve()
        self.prompt = Prompt(args.no_gui)
        self.sources = json.loads((ROOT / 'builder/sources.json').read_text())
        self.log = None

    def run(self, command, cwd=None, env=None):
        command = [str(item) for item in command]
        print('$ ' + shlex.join(command), flush=True)
        with subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
            for line in process.stdout:
                print(line, end='', flush=True)
                if self.log:
                    self.log.write(line)
                    self.log.flush()
            if process.wait():
                raise RuntimeError(f'Command failed ({process.returncode}): {shlex.join(command)}')

    def stage_source(self, stage):
        stage.mkdir()
        for name in ('app.py', 'acquisition.py', 'core.py', 'paths.py', 'channel_names.py', 'video_export.py', 'device_detection.py', 'shortcuts.py', 'reference_overlay.py',
                     'usb_setup.py', 'package_check.py', 'icon.svg', 'install-usb-access.sh', 'usb-setup-terminal.sh',
                     'README.md', 'TIMING.md'):
            shutil.copy2(ROOT / name, stage / name)
        for path in ROOT.glob('test_*.py'):
            shutil.copy2(path, stage / path.name)
        shutil.copytree(ROOT / 'packaging', stage / 'packaging')
        shutil.copytree(ROOT / 'builder', stage / 'builder', ignore=shutil.ignore_patterns('__pycache__'))

    def build(self):
        if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
            raise RuntimeError('This builder currently targets Linux x86_64.')
        with (self.work / '.build.lock').open('w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Another build is already using this working folder.') from None
            vendor = obtain_vendor(self.sources['vendor'], ROOT, self.work, self.prompt,
                                   self.args.vendor_zip, self.args.download_vendor)
            ensure_system(self.work, self.prompt, self.run, self.args.install_system_deps)
            fetched = {name: fetch(self.sources[name], self.work / 'downloads')
                       for name in ('driver', 'unshield', 'appimagetool', 'runtime', 'ffmpeg_license')}
            fetched['appimagetool'].chmod(0o755)
            run_root = Path(tempfile.mkdtemp(prefix='build-', dir=self.work))
            with (run_root / 'build.log').open('w') as log:
                self.log = log
                result = self.assemble(vendor, fetched, run_root)
                self.log = None
            return result

    def assemble(self, vendor, fetched, run_root):
        venv = self.work / 'venv'
        python = venv / 'bin/python'
        if venv.exists() and not usable_python(python):
            backup = venv.with_name('venv-old-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            venv.rename(backup)
            print(f'Preserved unusable build environment at {backup}', flush=True)
        if not python.exists():
            self.run([sys.executable, '-m', 'venv', venv])
        self.run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', ROOT / 'requirements-build.txt'])
        driver = extract_tar(fetched['driver'], run_root / 'driver-source')
        unshield_source = extract_tar(fetched['unshield'], run_root / 'unshield-source')
        unshield_build = run_root / 'unshield-build'
        self.run(['cmake', '-S', unshield_source, '-B', unshield_build, '-DBUILD_TESTING=OFF', '-DBUILD_STATIC=ON', '-DCMAKE_BUILD_TYPE=Release'])
        self.run(['cmake', '--build', unshield_build, '--parallel', str(min(os.cpu_count() or 2, 4))])
        setup = extract_setup(vendor, run_root / 'vendor-installer', self.sources['vendor']['setup_sha256'])
        cabinets = run_root / 'cabinets'
        extract(setup, cabinets)
        extracted = run_root / 'vendor-files'
        self.run([unshield_build / 'src/unshield', '-d', extracted, 'x', cabinets / 'data1.cab'])
        stage = run_root / 'application'
        self.stage_source(stage)
        adapt_driver(driver / 'hantek1008.py', stage / 'vendor/hantek1008.py', self.sources['driver']['driver_sha256'])
        shutil.copy2(driver / 'LICENSE', stage / 'vendor/LICENSE')
        self.run([python, '-m', 'builder.references', extracted, stage / 'references'], cwd=ROOT)
        native = stage / '.native'
        native.mkdir()
        shutil.copy2(fetched['ffmpeg_license'], native/'COPYING.GPLv3')
        self.run([python, '-c', 'import libusb_package,shutil,sys; p=libusb_package.get_library_path(); assert p, "No libusb library in installed wheel"; shutil.copy2(p, sys.argv[1])', native / 'libusb-1.0.so.0'])
        self.run([python, '-c', 'import imageio_ffmpeg,shutil,sys,subprocess,pathlib; p=imageio_ffmpeg.get_ffmpeg_exe(); shutil.copy2(p, sys.argv[1]); pathlib.Path(sys.argv[2]).write_text(subprocess.check_output([p,"-hide_banner","-L"], text=True, stderr=subprocess.STDOUT))',
                  native / 'ffmpeg', native / 'ffmpeg-license.txt'])
        test_env = dict(os.environ, QT_QPA_PLATFORM='offscreen', HANTEK_DATA_DIR=str(run_root / 'test-data'),
                        HANTEK_RECORDINGS_DIR=str(run_root / 'test-recordings'))
        self.run([python, '-m', 'unittest', 'discover', '-s', stage, '-v'], cwd=stage, env=test_env)
        self.run([python, '-m', 'PyInstaller', '--noconfirm', '--distpath', run_root / 'frozen',
                  '--workpath', run_root / 'pyinstaller', stage / 'packaging/hantek-linux-scope.spec'])
        appdir = run_root / 'Scope.AppDir'
        (appdir / 'usr/lib').mkdir(parents=True)
        shutil.copytree(run_root / 'frozen/hantek-linux-scope', appdir / 'usr/lib/hantek-linux-scope', symlinks=True)
        shutil.copy2(stage / 'packaging/AppRun', appdir / 'AppRun')
        (appdir / 'AppRun').chmod(0o755)
        shutil.copy2(stage / 'packaging/hantek-linux.desktop', appdir / 'hantek-linux.desktop')
        shutil.copy2(stage / 'icon.svg', appdir / 'hantek-linux.svg')
        (appdir / '.DirIcon').symlink_to('hantek-linux.svg')
        (appdir / 'usr/bin').mkdir()
        (appdir / 'usr/bin/hantek-linux-scope').symlink_to('../lib/hantek-linux-scope/hantek-linux-scope')
        compatibility = glibc_requirements(appdir)
        maximum_glibc = os.environ.get('TRACEFORGE_MAX_GLIBC')
        if maximum_glibc and (not compatibility['minimum_glibc'] or version_tuple(compatibility['minimum_glibc']) > version_tuple(maximum_glibc)):
            raise RuntimeError(f'Bundled libraries require glibc {compatibility["minimum_glibc"]}; target is {maximum_glibc}.')
        version = next(ast.literal_eval(item.value) for item in ast.parse((stage / 'app.py').read_text()).body
                       if isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'VERSION' for target in item.targets))
        name = f'TraceForge-{version}-x86_64.AppImage'
        pending = run_root / name
        self.run([fetched['appimagetool'], '--appimage-extract-and-run', '--no-appstream', '--runtime-file', fetched['runtime'],
                  '--mksquashfs-opt', '-processors', '--mksquashfs-opt', str(min(os.cpu_count() or 2, 4)), appdir, pending],
                 env=dict(os.environ, ARCH='x86_64'))
        pending.chmod(0o755)
        self.run([pending, '--appimage-extract-and-run', '--self-test', run_root / 'package-check.json'], cwd=run_root, env=test_env)
        packages = subprocess.check_output([python, '-m', 'pip', 'freeze'], text=True).splitlines()
        report = {'version': version, 'built_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'python': sys.version, 'platform': platform.platform(), 'libc': platform.libc_ver(),
                  'sources': self.sources, 'vendor_zip_sha256': sha256(vendor), 'python_packages': packages,
                  'driver_generated_sha256': sha256(stage / 'vendor/hantek1008.py'),
                  'ffmpeg_sha256': sha256(native / 'ffmpeg'),
                  'compatibility': compatibility,
                  'package_check': json.loads((run_root / 'package-check.json').read_text()),
                  'appimage_sha256': sha256(pending), 'build_log': str(run_root / 'build.log')}
        self.output.mkdir(parents=True, exist_ok=True)
        final = self.output / name
        temporary = self.output / ('.' + name + '.new')
        shutil.copy2(pending, temporary)
        temporary.replace(final)
        final.with_name(final.name + '.sha256').write_text(report['appimage_sha256'] + '  ' + final.name + '\n')
        final.with_name(final.name + '.build.json').write_text(json.dumps(report, indent=2) + '\n')
        print(f'\nBuild complete: {final}\nSHA256: {report["appimage_sha256"]}\nBuild log: {run_root / "build.log"}', flush=True)
        return final


def main():
    parser = argparse.ArgumentParser(description='Fetch dependencies, extract vendor assets, and build the Linux AppImage.')
    parser.add_argument('--work-dir', type=Path, default=ROOT / 'work')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    parser.add_argument('--vendor-zip', type=Path)
    parser.add_argument('--download-vendor', action='store_true', help='Download when no local vendor ZIP exists')
    parser.add_argument('--no-gui', action='store_true')
    parser.add_argument('--portable', action='store_true', help='Build in isolated Ubuntu 22.04 userspace using bubblewrap; downloads the build environment separately')
    parser.add_argument('--install-system-deps', action='store_true', help='Approve installation of missing build prerequisites')
    args = parser.parse_args()
    try:
        ensure_python(args, ROOT)
    except (Exception, KeyboardInterrupt) as exc:
        text = str(exc) or 'Build interrupted.'
        print(f'Python setup failed: {text}', file=sys.stderr, flush=True)
        Prompt(args.no_gui).notify(text, error=True)
        return 1
    if args.portable:
        from builder.portable import build_portable
        try:
            build_portable(args, ROOT)
        except (Exception, KeyboardInterrupt) as exc:
            print(f'Portable build failed: {exc}', file=sys.stderr, flush=True)
            return 1
        return 0
    builder = Builder(args)
    try:
        image = builder.build()
    except Cancelled as exc:
        print(str(exc), flush=True)
        return 2
    except (Exception, KeyboardInterrupt) as exc:
        text = str(exc) or 'Build interrupted.'
        print(f'Build failed: {text}', file=sys.stderr, flush=True)
        builder.prompt.notify(text, error=True)
        return 1
    builder.prompt.notify(f'AppImage built successfully:\n\n{image}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
