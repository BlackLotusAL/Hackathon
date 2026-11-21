"""Index and store replays from the shared practice directory and bundled samples."""
from functools import lru_cache
import os
from pathlib import Path
import re
import secrets
from tempfile import TemporaryDirectory, mkdtemp
import time

from game_core import mapgen
from game_core.protocol import dumps, json_object
from game_core.replay import FORMAT, game_result
from game_core.verification import verify_replay

from .. import KIT_ROOT


def data_directory(override=None):
    path = override or os.environ.get('ARENA_DATA_DIR')
    return (Path(path).expanduser() if path else Path.home() / '.exposure-field' / 'practice').resolve()


def new_run_directory(root):
    root.mkdir(parents=True, exist_ok=True)
    return Path(mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'), dir=root))


def _read_json(path):
    return json_object(path.read_bytes())


def _playable_replay(raw):
    replay = json_object(raw)
    if (replay.get('format') != FORMAT or replay.get('stage') != 'prelim'
            or not isinstance(replay.get('frames'), list)):
        raise ValueError('请输入 arena-replay/2 格式的初赛回放')
    if not isinstance(replay.get('map'), dict) or not isinstance(replay.get('init'), dict):
        raise ValueError('回放缺少地图或初始局面')
    return replay


def publish_replay(path, replay):
    """Expose the complete replay with a single rename on the same filesystem."""
    with TemporaryDirectory(prefix='.replay-', dir=path.parent) as temporary:
        staged = Path(temporary) / path.name
        staged.write_text(dumps(replay), encoding='utf-8')
        staged.replace(path)


def verify_replay_file(path, *, map_file=None, map_sha256=None):
    if (map_file is None) != (map_sha256 is None):
        raise ValueError('核对封存地图时请同时提供 --map-file 和 --map-sha256')
    replay = _read_json(Path(path))
    board = None
    if map_file is not None:
        sealed = {'file': Path(map_file).read_bytes().decode('utf-8'), 'sha256': map_sha256}
        board, _ = mapgen.load_map(sealed, 'prelim')
    return game_result(verify_replay(replay, board=board, require_public=True))


@lru_cache(maxsize=256)
def _replay_metadata(path, modified, size, inode):
    # File identity is part of the cache key so replacements refresh the listing.
    replay = _playable_replay(path.read_bytes())
    result = replay['result'] if isinstance(replay.get('result'), dict) else {}
    return dict(map=replay['map'].get('id'), players=replay.get('players'), stage=replay['stage'],
                **{key: result.get(key) for key in ('score', 'winner', 'reason', 'turns')})


def _valid_id(rid):
    return isinstance(rid, str) and rid not in ('.', '..') and bool(re.fullmatch(r'[\w.-]+', rid))


class ReplayStore:
    def __init__(self, override=None, *, kit=KIT_ROOT):
        self.root = data_directory(override)
        self.samples = kit / 'replays'

    def _location(self, rid):
        if not _valid_id(rid):
            raise KeyError(rid)
        if rid.startswith('imp-'):
            return self.root / 'imports' / (rid[4:] + '.json'), 'import'
        if rid.startswith('smp-'):
            return self.samples / (rid[4:] + '.json'), 'sample'
        return self.root / rid / 'replay.json', 'local'

    def list_replays(self):
        samples = sorted('smp-' + path.stem for path in self.samples.glob('*.json'))
        saved = ['imp-' + path.stem for path in (self.root / 'imports').glob('*.json')]
        if self.root.is_dir():
            saved.extend(path.name for path in self.root.iterdir()
                         if not path.name.startswith(('smp-', 'imp-')) and (path / 'replay.json').is_file())
        entries = []
        for rid in samples + sorted(saved, reverse=True):
            try:
                path, source = self._location(rid)
                stat = path.stat()
                fields = _replay_metadata(path, stat.st_mtime_ns, stat.st_size, stat.st_ino)
            except (OSError, KeyError, ValueError, RecursionError):
                continue
            entries.append(dict(fields, id=rid, source=source))
        return entries

    def stderr_path(self, rid, seat):
        if not _valid_id(rid):
            raise KeyError(rid)
        return self.root / rid / f'p{seat}.stderr.log'

    def load_replay(self, rid):
        path, _ = self._location(rid)
        try:
            return _playable_replay(path.read_bytes())
        except FileNotFoundError as error:
            raise KeyError(rid) from error

    def import_replay(self, raw):
        replay = _playable_replay(raw)
        rid = time.strftime('%m%d-%H%M%S') + '-' + secrets.token_hex(8)
        directory = self.root / 'imports'
        directory.mkdir(parents=True, exist_ok=True)
        publish_replay(directory / (rid + '.json'), replay)
        return 'imp-' + rid
