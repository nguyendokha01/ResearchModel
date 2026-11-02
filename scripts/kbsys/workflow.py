"""Small durable checkpoints and process-level writer locks, without a service."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from . import config

_LOCAL = threading.local()


class WriterBusy(ValueError):
    """Raised when a writer lock is held by another process."""

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def fingerprint(paths, extra=None):
    h = hashlib.sha256(json.dumps(extra, sort_keys=True, ensure_ascii=False).encode())
    for path in sorted(set(map(Path, paths)), key=str):
        h.update(str(path).encode())
        if path.is_file():
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
        else:
            h.update(b'<missing>')
    return h.hexdigest()

@contextmanager
def writer_lock(name='library', timeout=0):
    """O_EXCL is cross-process. Never steal a live or stale lock automatically."""
    held = getattr(_LOCAL, 'held', set())
    if name in held:
        yield
        return
    target = config.STORE / 'locks' / (hashlib.sha256(name.encode()).hexdigest()[:20] + '.lock')
    target.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    token = uuid.uuid4().hex
    while True:
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() - started >= timeout:
                raise WriterBusy(f'Another writer holds {name}. Inspect {target}; do not delete a live lock.')
            time.sleep(0.1)
    try:
        with os.fdopen(fd, 'w', newline='\n') as stream:
            json.dump({'pid': os.getpid(), 'token': token, 'name': name, 'created': config.now()}, stream)
        _LOCAL.held = held | {name}
        yield
    finally:
        _LOCAL.held = held
        if target.is_file() and json.loads(target.read_text()).get('token') == token:
            target.unlink()

def checkpoint_path(stage, scope):
    return config.STORE / 'workflow' / (hashlib.sha256((stage + ':' + scope).encode()).hexdigest()[:24] + '.json')

def checkpoint(stage, scope, inputs, outputs, operation, options=None):
    """Reuse only matching inputs AND outputs; errors/waiting never become cache hits."""
    path = checkpoint_path(stage, scope)
    input_paths = lambda: list(inputs() if callable(inputs) else inputs)
    output_paths = lambda: list(outputs() if callable(outputs) else outputs)
    ih = fingerprint(input_paths(), options)
    try:
        old = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        if not isinstance(old, dict):
            old = {}
    except (ValueError, OSError):
        old = {}  # corrupt derivative checkpoint is never a cache hit
    expected = [Path(p) for p in old.get('output_paths', [])] or output_paths()
    if (old.get('status') == 'done' and old.get('input_sha256') == ih
            and old.get('output_sha256') == fingerprint(expected) and all(p.is_file() for p in expected)
            and set(expected) == set(output_paths())
            and old.get('output_fingerprints') == {str(p): fingerprint([p]) for p in expected}):
        return {**old, 'cache_hit': True}
    state = {'schema_version': 1, 'stage': stage, 'scope': scope, 'status': 'running',
             'input_sha256': ih, 'started': config.now(), 'cache_hit': False}
    atomic_json(path, state)
    try:
        result = operation()
        state['result'] = result
        if isinstance(result, dict) and result.get('error'):
            raise ValueError(str(result['error']))
        waiting = isinstance(result, dict) and (result.get('skipped') or result.get('waiting') or result.get('failed') or result.get('errors'))
        completed_outputs = output_paths()
        missing = [str(p) for p in completed_outputs if not p.is_file()]
        waiting = bool(waiting or missing)
        state['status'] = 'waiting' if waiting else 'done'
        state['missing_outputs'] = missing
        state['input_sha256'] = fingerprint(input_paths(), options)
        state['output_paths'] = [str(p) for p in completed_outputs]
        state['output_sha256'] = fingerprint(completed_outputs)
        state['output_fingerprints'] = {str(p): fingerprint([p]) for p in completed_outputs}
        state['ended'] = config.now()
        atomic_json(path, state)
        return state
    except Exception as exc:
        state.update(status='failed', error=f'{type(exc).__name__}: {exc}', ended=config.now())
        atomic_json(path, state)
        raise

def status():
    folder = config.STORE / 'workflow'
    results = []
    for path in sorted(folder.glob('*.json')):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError('Expected checkpoint object')
            results.append(value)
        except (ValueError, OSError) as exc:
            results.append({'status': 'invalid', 'path': str(path), 'error': str(exc), 'cache_hit': False})
    return results
