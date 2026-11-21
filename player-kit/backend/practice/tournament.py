"""Round-robin scheduling for the command line and live practice."""
from collections import defaultdict
from itertools import combinations
import json
from multiprocessing import Pool
import os
from pathlib import Path
import random
import secrets
from tempfile import NamedTemporaryFile
import threading
import time
import traceback
from typing import NamedTuple

from game_core import mapgen

from ..runtime.local import bot_name, fast_spec
from .catalog import load_maps
from .match import Match
from .replay_store import data_directory


class Pairing(NamedTuple):
    map_id: str
    p0: int
    p1: int


def random_map_ids(count, seed):
    rng = random.Random(seed)
    return [f'R{rng.randrange(1, 10 ** 6)}' for _ in range(count)]


def round_robin(bot_count, map_ids):
    return [Pairing(map_id, p0, p1)
            for left, right in combinations(range(bot_count), 2)
            for map_id in map_ids
            for p0, p1 in ((left, right), (right, left))]


def resolve_boards(map_ids, catalog, stage='prelim'):
    return {map_id: mapgen.get(map_id, stage, catalog) for map_id in dict.fromkeys(map_ids)}


def _match_record(pairing, result):
    return {'map': pairing.map_id, 'p0': pairing.p0, 'p1': pairing.p1,
            'winner': result['winner'], 'score': result['score'], 'reason': result['reason'],
            'errors': result.get('errors')}


def _play_job(payload):
    pairing, board, specs, timing = payload
    result = Match(board, specs, timing=timing).run()
    return dict(_match_record(pairing, result), bonus=result['bonus'])


class BatchTournament:
    def __init__(self, bots, *, map_ids=None, count=6, seed=None, fast=False, workers=None):
        if len(bots) < 2:
            raise ValueError('至少需要两个 Bot')
        self.bots = bots
        self.names = [bot_name(bot) for bot in bots]
        self.map_ids = map_ids or random_map_ids(count, seed)
        self.jobs = round_robin(len(bots), self.map_ids)
        self.fast = fast
        self.workers = workers or ((os.cpu_count() or 2) if fast else 1)
        self._boards = resolve_boards(self.map_ids, load_maps())
        self._specs = [fast_spec(bot) for bot in bots] if fast and self.jobs else bots

    def run(self, data_dir=None, *, on_progress=None):
        tasks = ((job, self._boards[job.map_id], [self._specs[job.p0], self._specs[job.p1]], not self.fast)
                 for job in self.jobs)
        records = []
        with Pool(self.workers) as pool:
            for result in pool.imap_unordered(_play_job, tasks):
                records.append(dict(result, p0=self.bots[result['p0']], p1=self.bots[result['p1']]))
                if on_progress is not None:
                    on_progress(len(records))
        root = data_directory(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=root, delete=False,
                                prefix=time.strftime('tournament_%Y%m%d-%H%M%S-'), suffix='.json') as output:
            json.dump(records, output, indent=1)
        return records, Path(output.name)

    def head_to_head(self, records):
        groups = defaultdict(list)
        for record in records:
            groups[frozenset((record['p0'], record['p1']))].append(record)
        table = []
        for left in self.bots:
            row = []
            for right in self.bots:
                if left == right:
                    row.append(None)
                    continue
                matches = groups.get(frozenset((left, right)), ())
                wins = losses = diff = 0
                for game in matches:
                    seat = 0 if game['p0'] == left else 1
                    wins += game['winner'] == seat
                    losses += game['winner'] == 1 - seat
                    diff += game['score'][seat] - game['score'][1 - seat]
                row.append((wins, losses, diff / max(1, len(matches))))
            table.append(row)
        return table


class LiveTournament:
    def __init__(self, names, map_ids, make_match):
        self.id = 't' + time.strftime('%m%d-%H%M%S') + '-' + secrets.token_hex(8)
        self.names = names
        self.maps = sorted(set(map_ids))
        self.jobs = round_robin(len(names), map_ids)
        self._make_match = make_match
        self._lock = threading.Lock()
        self._done = []
        self._running = None
        self._stopped = False
        self._finished = False
        self._error = None

    def run(self):
        try:
            for job in self.jobs:
                with self._lock:
                    if self._stopped:
                        break
                    live = self._make_match(job, self.id)
                    self._running = live
                live.run()
                result = live.result or {'winner': None, 'score': [0, 0], 'reason': 'error',
                                         'errors': [0, 0], 'time_us': [0, 0]}
                record = dict(_match_record(job, result), match=live.id, time_us=result.get('time_us'))
                with self._lock:
                    self._done.append(record)
                    self._running = None
        except Exception as error:
            with self._lock:
                self._error = str(error)
            traceback.print_exc()
        finally:
            with self._lock:
                self._running = None
                self._finished = True

    def stop(self):
        with self._lock:
            self._stopped = True
            live = self._running
        if live is not None:
            live.control('stop')

    def _table(self):
        rows = [{'name': name, 'w': 0, 'l': 0, 'diff': 0, 'errors': 0} for name in self.names]
        for game in self._done:
            for seat, player in enumerate((game['p0'], game['p1'])):
                row = rows[player]
                if game['winner'] == seat:
                    row['w'] += 1
                elif game['winner'] in (0, 1):
                    row['l'] += 1
                row['diff'] += game['score'][seat] - game['score'][1 - seat]
                row['errors'] += (game['errors'] or [0, 0])[seat]
        order = sorted(range(len(rows)), key=lambda i: (-rows[i]['w'], -rows[i]['diff']))
        return [dict(rows[i], rank=rank, idx=i) for rank, i in enumerate(order, 1)]

    def status(self):
        with self._lock:
            return {'id': self.id, 'names': self.names, 'maps': self.maps, 'total': len(self.jobs),
                    'done': list(self._done), 'running': self._running.id if self._running else None,
                    'table': self._table(), 'stopped': self._stopped, 'error': self._error,
                    'finished': self._finished}
