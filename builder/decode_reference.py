#!/usr/bin/env python3
"""Decode the HTK-DSO-AM reference format found in Hantek Scope1008 1.0.30.

Usage: python3 decode_hantek_amr.py input.amr output.json
Preserves stored sample values. Time units and physical amplitude units are
not encoded in the structure recovered here and must not be assumed.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct

SIGNATURE = 'HTK-DSO-AM'.encode('utf-16-le')


def decode_amr(path):
    path = Path(path)
    data = path.read_bytes()
    if len(data) < 26 or data[:20] != SIGNATURE:
        raise ValueError('Unsupported reference format: expected UTF-16LE HTK-DSO-AM')
    trace_count, sample_count = struct.unpack_from('<HI', data, 20)
    if not 1 <= trace_count <= 8 or not 0 < sample_count <= 10_000_000:
        raise ValueError('Invalid trace/sample count')
    expected = 26 + trace_count * (16 + 4 * sample_count)
    if len(data) != expected:
        raise ValueError(f'Length mismatch: expected {expected}, got {len(data)}')
    traces = []
    offset = 26
    for index in range(trace_count):
        upper, lower = struct.unpack_from('<dd', data, offset)
        offset += 16
        values = struct.unpack_from(f'<{sample_count}f', data, offset)
        offset += sample_count * 4
        if not all(math.isfinite(v) for v in (upper, lower, *values)):
            raise ValueError('Non-finite sample or bound')
        traces.append({'trace_index': index, 'stored_upper_bound': upper,
                       'stored_lower_bound': lower, 'samples': list(values)})
    return {'source_filename': path.name, 'sha256': hashlib.sha256(data).hexdigest(),
            'format': 'HTK-DSO-AM', 'trace_count': trace_count,
            'samples_per_trace': sample_count,
            'axis_note': 'Use sample index on the x axis. Physical units and sample interval are unverified.',
            'traces': traces}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = decode_amr(args.input)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(f"Decoded {result['trace_count']} trace(s), {result['samples_per_trace']} samples each")
