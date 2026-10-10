import argparse
import json
from pathlib import Path

import numpy as np

from builder.decode_reference import decode_amr


def make_references(source, target):
    target.mkdir(parents=True, exist_ok=True)
    arrays, entries, names = {}, [], set()
    for path in sorted(source.rglob('*')):
        if path.suffix.lower() != '.amr':
            continue
        if path.name in names:
            raise ValueError(f'Duplicate vendor example: {path.name}')
        names.add(path.name)
        try:
            decoded = decode_amr(path)
        except ValueError as exc:
            entries.append({'source_filename': path.name, 'status': 'unsupported', 'reason': str(exc)})
            continue
        traces = []
        for number, trace in enumerate(decoded['traces'], 1):
            key = f'{path.stem}__trace_{number}'
            values = np.asarray(trace['samples'], dtype=np.float32)
            arrays[key] = values
            traces.append({'npz_key': key, 'stored_upper_bound': trace['stored_upper_bound'],
                           'stored_lower_bound': trace['stored_lower_bound'],
                           'sample_min': float(values.min()), 'sample_max': float(values.max())})
        decoded.update(status='decoded', traces=traces)
        entries.append(decoded)
    count = sum(entry['status'] == 'decoded' for entry in entries)
    if count != 86 or len(arrays) != 121:
        raise ValueError(f'Unexpected vendor examples: {count} examples, {len(arrays)} traces; expected 86/121')
    np.savez_compressed(target / 'reference-waveforms.npz', **arrays)
    (target / 'reference-index.json').write_text(json.dumps(entries, indent=2) + '\n')
    print(f'Decoded {count} vendor examples ({len(arrays)} traces).', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('target', type=Path)
    args = parser.parse_args()
    make_references(args.source, args.target)
