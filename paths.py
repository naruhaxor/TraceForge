"""Keep writable recordings separate from read-only installed resources."""
import os
import datetime as dt
import json
from pathlib import Path
import tempfile


def data_directory():
    override = os.environ.get("HANTEK_DATA_DIR")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("HANTEK_DATA_DIR must be an absolute path")
        return path
    xdg = Path(os.environ.get("XDG_DATA_HOME", ""))
    base = xdg if xdg.is_absolute() else Path.home() / ".local" / "share"
    return base / "hantek-linux"


def default_recordings_directory():
    return Path.home() / 'Documents' / 'Traceforge'


def recordings_directory():
    override = os.environ.get('HANTEK_RECORDINGS_DIR')
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError('HANTEK_RECORDINGS_DIR must be an absolute path')
        return path
    try:
        saved = json.loads((data_directory() / 'recording-settings.json').read_text())
        value = saved.get('directory')
        if saved.get('version') == 1 and isinstance(value, str) and Path(value).is_absolute():
            return Path(value)
    except (OSError, ValueError, AttributeError):
        pass
    return default_recordings_directory()


def set_recordings_directory(directory):
    path = Path(directory).expanduser()
    if not path.is_absolute():
        raise ValueError('Choose an absolute recording-folder path')
    path.mkdir(parents=True, exist_ok=True)
    
    with tempfile.TemporaryFile(dir=path) as probe:
        probe.write(b'Hantek recording-folder check')
        probe.flush()
    settings = data_directory() / 'recording-settings.json'
    settings.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=settings.parent, prefix='.recording-settings-', delete=False) as file:
            temporary = Path(file.name)
            json.dump({'version': 1, 'directory': str(path)}, file, indent=2)
            file.write('\n')
        temporary.replace(settings)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return path


def recording_filename(name):
    name = name.strip()
    if name.lower().endswith('.hscope'):
        name = name[:-7].rstrip()
    if not name or name in {'.', '..'}:
        raise ValueError('Enter a recording name.')
    if any(ord(char) < 32 or ord(char) == 127 or char in '<>:"/\\|?*' for char in name):
        raise ValueError('Use a filename without slashes, control characters, or <>:"|?*.')
    if len(name.encode('utf-8')) > 200:
        raise ValueError('Use a shorter recording name (up to 200 UTF-8 bytes).')
    return name + '.hscope'


def new_recording_path(name, now=None, part=1):
    filename = recording_filename(name)
    if part > 1:
        filename = f'{filename[:-7]}-part-{part:02}.hscope'
    now = now or dt.datetime.now()
    day = recordings_directory() / now.strftime('%Y-%m-%d')
    path = day / filename
    if path.exists() or path.is_symlink():
        raise FileExistsError('A recording with this name already exists for this date. Choose another name.')
    return path
