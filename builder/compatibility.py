from pathlib import Path
import re
import subprocess


def glibc_requirements(root):
    requirements = {}
    for path in Path(root).rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                continue
        result = subprocess.run(['readelf', '--version-info', str(path)], capture_output=True, text=True, check=True)
        sections = result.stdout.split('Version needs section', 1)
        if len(sections) < 2:
            continue
        versions = re.findall(r'Name: GLIBC_(\d+(?:\.\d+)+)', sections[1])
        if versions:
            requirements[str(path.relative_to(root))] = max(versions, key=version_tuple)
    minimum = max(requirements.values(), key=version_tuple) if requirements else None
    return {'minimum_glibc': minimum, 'elf_requirements': requirements,
            'note': 'Symbol-version audit only; target display, graphics and USB behavior require distribution testing.'}


def version_tuple(value):
    return tuple(map(int, value.split('.')))
