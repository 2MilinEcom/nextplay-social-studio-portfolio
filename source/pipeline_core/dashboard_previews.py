"""Local, disposable poster cache. Never fetches URLs or rewrites source media."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from urllib.parse import urlencode
from ffmpeg_bin import FFMPEG

_workers = threading.BoundedSemaphore(2)
_lock_guard = threading.Lock()
_locks = {}
VIDEO_TYPES = {'.mp4', '.mov', '.m4v', '.webm'}

def local_video(root, value):
    root = Path(root).resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    path.relative_to(root)
    if path.suffix.lower() not in VIDEO_TYPES or not path.is_file():
        raise ValueError('Downloaded source unavailable')
    return path

def fingerprint(root, path):
    stat = path.stat()
    key = f"{path.relative_to(Path(root).resolve())}:{stat.st_size}:{stat.st_mtime_ns}"
    return hashlib.sha256(key.encode()).hexdigest()

def poster_url(root, video_id, value):
    if not value:
        return None
    try:
        path = local_video(root, value)
        return '/api/source-poster?' + urlencode({'video_id': str(video_id), 'v': fingerprint(root, path)})
    except (OSError, ValueError):
        return None

def make_poster(root, value):
    root = Path(root).resolve()
    source = local_video(root, value)
    cache = root / 'nextplay' / 'dashboard-posters'
    # Check before creating anything, including nonexistent descendants of links.
    cache.resolve().relative_to(root)
    cache.mkdir(parents=True, exist_ok=True)
    cache = cache.resolve()
    target = cache / (fingerprint(root, source) + '.jpg')
    with _lock_guard:
        lock = _locks.setdefault(str(target), threading.Lock())
    with lock:
        if target.is_symlink():
            raise ValueError('Unsafe preview cache')
        if target.is_file():
            return target
        with _workers:
            fd, temporary = tempfile.mkstemp(prefix='poster-', suffix='.jpg', dir=cache)
            os.close(fd)
            temporary = Path(temporary)
            try:
                for seek in (.25, 0):
                    result = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error',
                        '-threads', '1', '-ss', str(seek), '-i', str(source), '-vf',
                        'scale=480:640:force_original_aspect_ratio=decrease',
                        '-frames:v', '1', '-threads', '1', '-q:v', '4', '-y', str(temporary)],
                        capture_output=True, timeout=30)
                    if result.returncode == 0 and temporary.stat().st_size > 0:
                        break
                else:
                    raise ValueError('Could not generate preview')
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    return target
