import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile

from builder.downloads import fetch


def usable_python(python):
    try:
        result = subprocess.run([str(python), '-I', '-c',
                                 'import sys,ensurepip,venv,ssl,ctypes,zlib; '
                                 'raise SystemExit(not (3,12) <= sys.version_info[:2] < (3,15))'],
                                capture_output=True, timeout=30)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def extract_python(archive, target):
    target.mkdir()
    try:
        with tarfile.open(archive) as source:
            entries = source.getmembers()
            if sum(entry.size for entry in entries) > 512 * 1024 * 1024:
                raise ValueError('Unexpected Python archive size')
            seen, links = set(), []
            for entry in entries:
                path = PurePosixPath(entry.name)
                if (path.is_absolute() or '..' in path.parts or not path.parts
                        or path.parts[0] != 'python' or path in seen):
                    raise ValueError('Unsafe Python archive path')
                seen.add(path)
                if entry.issym():
                    link = PurePosixPath(entry.linkname)
                    destination = (target / path.parent / link).resolve()
                    if link.is_absolute() or os.path.commonpath((str(target.resolve()), str(destination))) != str(target.resolve()):
                        raise ValueError('Unsafe Python archive link')
                    links.append(entry)
                elif not (entry.isfile() or entry.isdir()):
                    raise ValueError('Unsupported Python archive entry')
            link_paths = {PurePosixPath(entry.name) for entry in links}
            for entry in entries:
                path = PurePosixPath(entry.name)
                if any(parent in link_paths for parent in path.parents):
                    raise ValueError('Python archive writes through a link')
                output = target / path
                if entry.isdir():
                    output.mkdir(parents=True, exist_ok=True)
                elif entry.isfile():
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(entry) as content, output.open('xb') as stream:
                        shutil.copyfileobj(content, stream)
                    output.chmod(entry.mode & 0o777)
            for entry in links:
                output = target / entry.name
                output.parent.mkdir(parents=True, exist_ok=True)
                output.symlink_to(entry.linkname)
            for entry in links:
                resolved = (target / entry.name).resolve(strict=True)
                if os.path.commonpath((str(target.resolve()), str(resolved))) != str(target.resolve()):
                    raise ValueError('Python archive link escapes installation')
    except BaseException:
        shutil.rmtree(target)
        raise


def private_python(work, specification):
    work.mkdir(parents=True, exist_ok=True)
    target = work / ('python-' + specification['sha256'][:16])
    python = target / 'python/bin/python3'
    with (work / '.python-bootstrap.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if python.exists() and usable_python(python):
            print('Using private build Python: ' + str(python), flush=True)
            return python
        archive = fetch(specification, work / 'downloads')
        temporary = Path(tempfile.mkdtemp(prefix='.python-', dir=work))
        try:
            extract_python(archive, temporary / 'install')
            candidate = temporary / 'install/python/bin/python3'
            if not usable_python(candidate):
                raise RuntimeError('Downloaded Python cannot run on this system. This builder needs x86_64 Linux with glibc.')
            if target.exists():
                shutil.rmtree(target)
            (temporary / 'install').replace(target)
        finally:
            shutil.rmtree(temporary)
    return python


def ensure_python(args, source):
    if os.geteuid() == 0:
        raise RuntimeError('Run the builder as your normal user. Only missing system packages use sudo.')
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise RuntimeError('This builder currently targets Linux x86_64.')
    specification = json.loads((source / 'builder/sources.json').read_text())['python']
    work = args.work_dir.expanduser().resolve()
    target = work / ('python-' + specification['sha256'][:16]) / 'python/bin/python3'
    if usable_python(sys.executable) and (not args.portable or Path(sys.executable).resolve() == target.resolve()):
        return
    print('Setting up a private Python with pip and venv in the build folder. System Python stays unchanged.', flush=True)
    python = private_python(work, specification)
    os.execv(str(python), [str(python), str(source / 'build.py'), *sys.argv[1:]])
