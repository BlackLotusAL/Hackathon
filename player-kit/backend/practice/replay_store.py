"""Index and store replays from the shared practice directory and bundled samples."""
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


def verify_replay_file(path, *, map_file=None, map_sha256=None):
    if (map_file is None) != (map_sha256 is None):
        raise ValueError('核对封存地图时请同时提供 --map-file 和 --map-sha256')
    replay = _read_json(Path(path))
    board = None
    if map_file is not None:
        sealed = {'file': Path(map_file).read_bytes().decode('utf-8'), 'sha256': map_sha256}
        board, _ = mapgen.load_map(sealed, 'prelim')
    return game_result(verify_replay(replay, board=board, require_public=True))


def _replay_metadata(replay):
    result = replay['result'] if isinstance(replay.get('result'), dict) else {}
    board = replay['map'] if isinstance(replay.get('map'), dict) else {}
    return dict(map=board.get('id'), players=replay.get('players'), stage=replay.get('stage'),
                **{key: result.get(key) for key in ('score', 'winner', 'reason', 'turns')})


def _valid_id(rid):
    return isinstance(rid, str) and rid not in ('.', '..') and bool(re.fullmatch(r'[\w.-]+', rid))


class ReplayStore:
    def __init__(self, override=None, *, kit=KIT_ROOT):
        self.root = data_directory(override)
        self.samples = kit / 'replays'
        legacy = kit / 'runs'
        self.roots = tuple(dict.fromkeys((self.root, legacy / 'server', legacy)))

    def list_replays(self):
        samples = []
        for path in sorted(self.samples.glob('*.json')):
            rid = 'smp-' + path.stem
            if _valid_id(rid):
                try:
                    samples.append(dict(_replay_metadata(_read_json(path)), id=rid, source='sample'))
                except (OSError, ValueError, RecursionError):
                    continue

        entries = {}
        for root in self.roots:
            for path in (root / 'imports').glob('*.json'):
                rid = 'imp-' + path.stem
                if _valid_id(rid) and rid not in entries:
                    try:
                        fields = _read_json(path.with_suffix('.meta'))
                    except (OSError, ValueError, RecursionError):
                        try:
                            fields = _replay_metadata(_read_json(path))
                        except (OSError, ValueError, RecursionError):
                            continue
                    entries[rid] = dict(fields, id=rid, source='import')
            if not root.is_dir():
                continue
            for path in root.iterdir():
                rid = path.name
                if (rid in entries or not _valid_id(rid) or rid.startswith(('smp-', 'imp-'))
                        or not (path / 'replay.json').is_file()):
                    continue
                try:
                    result = _read_json(path / 'result.json')
                except (OSError, ValueError, RecursionError):
                    continue
                fields = {key: result.get(key) for key in ('map', 'players', 'score', 'winner', 'reason', 'turns', 'stage')}
                entries[rid] = dict(fields, id=rid, source='local')
        return samples + [entries[rid] for rid in sorted(entries, reverse=True)]

    def _local_path(self, relative):
        for root in self.roots:
            path = root / relative
            if path.is_file():
                return path
        return self.root / relative

    def replay_path(self, rid):
        if not _valid_id(rid):
            raise KeyError(rid)
        if rid.startswith('imp-'):
            return self._local_path(Path('imports') / (rid[4:] + '.json'))
        if rid.startswith('smp-'):
            return self.samples / (rid[4:] + '.json')
        return self._local_path(Path(rid) / 'replay.json')

    def stderr_path(self, rid, seat):
        if not _valid_id(rid):
            raise KeyError(rid)
        return self._local_path(Path(rid) / f'p{seat}.stderr.log')

    def load_replay(self, rid):
        return _read_json(self.replay_path(rid))

    def import_replay(self, raw):
        replay = json_object(raw)
        if (replay.get('format') != FORMAT or replay.get('stage') != 'prelim'
                or not isinstance(replay.get('frames'), list)):
            raise ValueError('请输入 arena-replay/2 格式的初赛回放')
        if not isinstance(replay.get('map'), dict) or not isinstance(replay.get('init'), dict):
            raise ValueError('回放缺少地图或初始局面')
        rid = time.strftime('%m%d-%H%M%S') + '-' + secrets.token_hex(8)
        directory = self.root / 'imports'
        directory.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='import-', dir=directory) as temp:
            staged = Path(temp) / (rid + '.json')
            staged.write_text(dumps(replay), encoding='utf-8')
            metadata = staged.with_suffix('.meta')
            metadata.write_text(dumps(_replay_metadata(replay)), encoding='utf-8')
            destination = directory / metadata.name
            metadata.replace(destination)
            try:
                staged.replace(directory / staged.name)
            except OSError:
                destination.unlink(missing_ok=True)
                raise
        return 'imp-' + rid
