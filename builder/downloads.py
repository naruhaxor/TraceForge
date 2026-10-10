import hashlib
from pathlib import Path
import shutil
import tarfile
import time
import urllib.request
import zipfile


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify(path, expected):
    actual = sha256(path)
    if actual != expected:
        raise ValueError(f'Checksum mismatch for {path.name}: expected {expected}, got {actual}. The upstream file may have changed; update the reviewed source lock before building.')


def fetch(source, folder):
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source['filename']
    if target.exists():
        verify(target, source['sha256'])
        print(f'Using verified download: {target.name}', flush=True)
        return target
    if not source['url'].startswith('https://'):
        raise ValueError('Dependency URLs must use HTTPS')
    temporary = target.with_name(target.name + '.part')
    print(f'Downloading {source["url"]}\n  → {target}', flush=True)
    try:
        request = urllib.request.Request(source['url'], headers={'User-Agent': 'Hantek-Linux-Builder/0.5'})
        with urllib.request.urlopen(request, timeout=30) as response, temporary.open('wb') as file:
            if not response.url.startswith('https://'):
                raise ValueError('Download redirected away from HTTPS')
            total = int(response.headers.get('Content-Length', 0))
            received, previous = 0, time.monotonic()
            while block := response.read(256 * 1024):
                received += len(block)
                if received > 512 * 1024 * 1024:
                    raise ValueError('Unexpected dependency size')
                file.write(block)
                if time.monotonic() - previous >= 2:
                    print(f'  {received / 1048576:.1f} MiB' + (f' / {total / 1048576:.1f} MiB' if total else ''), flush=True)
                    previous = time.monotonic()
        verify(temporary, source['sha256'])
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def obtain_vendor(source, root, work, prompt, selected=None, download=False):
    target = work / source['filename']
    if selected is not None:
        selected = Path(selected).expanduser().resolve()
    else:
        selected = next((p for p in (target, root / source['filename'], Path.cwd() / source['filename']) if p.is_file()), None)
    if selected is None:
        selected = 'download' if download else prompt.choose_vendor(source['url'])
    if selected == 'download':
        return fetch(source, work)
    selected = Path(selected)
    verify(selected, source['sha256'])
    if selected.resolve() != target.resolve():
        temporary = target.with_name(target.name + '.part')
        try:
            shutil.copyfile(selected, temporary)
            verify(temporary, source['sha256'])
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    print(f'Using vendor ZIP: {target}', flush=True)
    return target


def extract_setup(archive, destination, expected_sha):
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        matches = [entry for entry in zipped.infolist() if Path(entry.filename.replace('\\', '/')).name.lower() == 'setup.exe' and not entry.is_dir()]
        if len(matches) != 1 or not 0 < matches[0].file_size < 200 * 1024 * 1024:
            raise ValueError('Expected exactly one supported Setup.EXE in the Hantek ZIP')
        target = destination / 'Setup.EXE'
        with zipped.open(matches[0]) as source, target.open('xb') as output:
            shutil.copyfileobj(source, output)
    verify(target, expected_sha)
    return target


def extract_tar(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        entries = tar.getmembers()
        if sum(item.size for item in entries) > 100 * 1024 * 1024:
            raise ValueError('Unexpected source archive size')
        for entry in entries:
            if not (entry.isfile() or entry.isdir()) or entry.name.startswith('/') or '..' in Path(entry.name).parts:
                raise ValueError('Unsafe source archive entry')
        tar.extractall(destination, filter='data')
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise ValueError('Unexpected source archive layout')
    return children[0]
