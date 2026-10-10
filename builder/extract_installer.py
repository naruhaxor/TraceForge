#!/usr/bin/env python3
"""Extract this installer's Unicode InstallShield overlay without executing it.

Usage: python3 extract_hantek_setup.py Setup.EXE output-directory
The output cabinets can then be unpacked with Unshield.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct


def extract(source, destination):
    source, destination = Path(source), Path(destination)
    data = source.read_bytes()
    if len(data) < 64 or data[:2] != b'MZ':
        raise ValueError('Expected a PE executable')
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if data[pe:pe+4] != b'PE\0\0':
        raise ValueError('Missing PE signature')
    sections = struct.unpack_from('<H', data, pe+6)[0]
    optional_size = struct.unpack_from('<H', data, pe+20)[0]
    table = pe+24+optional_size
    ends = []
    for index in range(sections):
        size, offset = struct.unpack_from('<II', data, table+40*index+16)
        ends.append(size+offset)
    position = max(ends)
    count = struct.unpack_from('<I', data, position)[0]
    position += 4
    if not 0 < count < 1000:
        raise ValueError('Unsupported overlay header')
    records = []
    names = set()
    for index in range(count):
        fields = []
        for field in range(4):
            start = position
            while position+2 <= len(data) and data[position:position+2] != b'\0\0':
                position += 2
            if position+2 > len(data):
                raise ValueError('Unterminated header field')
            fields.append(data[start:position].decode('utf-16-le'))
            position += 2
        name, original_path, version, size = fields
        size = int(size)
        if not name or name in {'.', '..'} or '/' in name or '\\' in name or name in names:
            raise ValueError('Unsafe or duplicate embedded filename')
        names.add(name)
        if not 0 <= size <= len(data)-position:
            raise ValueError('Embedded file extends beyond installer')
        records.append({'name': name, 'original_path': original_path,
                        'version': version, 'offset': position, 'size': size,
                        'sha256': hashlib.sha256(data[position:position+size]).hexdigest()})
        position += size
    if position != len(data):
        raise ValueError('Unexpected data after embedded files')
    destination.mkdir(parents=True, exist_ok=True)
    for record in records:
        target = destination / record['name']
        if target.exists():
            raise FileExistsError(f'Will not overwrite {target}')
    for record in records:
        offset, size = record['offset'], record['size']
        (destination/record['name']).write_bytes(data[offset:offset+size])
    (destination/'embedded-manifest.json').write_text(json.dumps(records, indent=2)+'\n')
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    records = extract(args.source, args.destination)
    print(f'Extracted {len(records)} embedded files; complete installer payload accounted for')
