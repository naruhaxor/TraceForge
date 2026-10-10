#!/usr/bin/env python3
import argparse
import ast
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent


def package(destination):
    files = json.loads((ROOT / 'source-files.json').read_text())
    version = next(ast.literal_eval(item.value) for item in ast.parse((ROOT / 'app.py').read_text()).body
                   if isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'VERSION' for target in item.targets))
    paths = []
    for name in files:
        path = ROOT / name
        if Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink() or not path.is_file():
            raise ValueError(f'Invalid source package entry: {name}')
        paths.append(path)
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f'TraceForge-{version}-source.zip'
    temporary = output.with_suffix('.zip.part')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in paths:
            archive.write(path, Path(f'TraceForge-{version}-source') / path.relative_to(ROOT))
    temporary.replace(output)
    with output.open('rb') as file:
        digest = hashlib.file_digest(file, 'sha256').hexdigest()
    output.with_name(output.name + '.sha256').write_text(digest + '  ' + output.name + '\n')
    print(f'Source package: {output}\n{len(paths)} own-source files; SHA256 {digest}')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Package only the explicit source manifest; exclude every downloaded dependency and generated asset.')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    package(args.output_dir.expanduser().resolve())
