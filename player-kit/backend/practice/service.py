"""Manage one local practice instance and its background sessions."""
import os
import re
import threading

from game_core import mapgen

from .. import KIT_ROOT
from ..runtime.local import bot_name
from ..runtime.submission import submission_language
from .catalog import BOTS_DIR, load_maps
from .check import check
from .live import HumanBot, LiveMatch
from .replay_store import ReplayStore
from .tournament import LiveTournament, random_map_ids, resolve_boards


class Practice:
    def __init__(self, data_dir=None):
        self.replays = ReplayStore(data_dir)
        self.maps = load_maps()
        self.cwd = os.getcwd()
        self._lock = threading.Lock()
        self._matches = {}
        self._tournaments = {}
        self._closed = False

    def map_list(self):
        boards = sorted(self.maps.values(), key=lambda board: (board.get('stage') != 'prelim', board['id']))
        return [{key: board.get(key) for key in ('id', 'name', 'stage')} for board in boards]

    def get_map(self, name, stage='prelim'):
        return mapgen.get(name, stage, self.maps)

    def check_bot(self, target, *, quick=False):
        target = (target or '').strip()
        if not target:
            raise ValueError('请填写提交目录、启动命令或 HTTP(S) 地址')
        if re.fullmatch(r'[\w.-]+', target) and (BOTS_DIR / target).is_dir():
            target = str(BOTS_DIR / target)
        return check(target, cwd=self.cwd, quick=quick)

    def _player(self, config):
        kind = config.get('type')
        if kind == 'bot':
            name = config['id']
            if name in ('.', '..') or not re.fullmatch(r'[\w.-]+', name):
                raise ValueError('Bot 目录名无效')
            directory = BOTS_DIR / name
            language = submission_language(directory)
            if config.get('fast') and language == 'python':
                return f'inproc:{directory}', name, KIT_ROOT
            return str(directory), name, directory
        if kind == 'url':
            url = config['url'].strip().rstrip('/')
            if not re.fullmatch(r'https?://[^/\s]+', url):
                raise ValueError('地址应形如 http://127.0.0.1:9000')
            return url, config.get('name') or bot_name(url), None
        if kind == 'cmd':
            command = config['cmd'].strip()
            if not command:
                raise ValueError('启动命令为空')
            cwd = config.get('cwd') or self.cwd
            return command, config.get('name') or bot_name(command, cwd), cwd
        if kind == 'human':
            return HumanBot(), config.get('name') or HumanBot.name, None
        raise ValueError(f'未知的对战方类型：{kind!r}')

    def _register_match(self, config, board, players, *, start_from=None, tournament=None):
        with self._lock:
            if self._closed:
                raise ValueError('练习服务正在关闭')
            live = LiveMatch(config, board, players, self.replays.root, start_from=start_from, tournament=tournament)
            self._matches[live.id] = live
        return live

    def start_match(self, config):
        sides = config['players']
        if len(sides) < 2:
            raise ValueError('请提供两个对战方')
        players = [self._player(sides[seat]) for seat in (0, 1)]
        start_from = None
        if config.get('from_replay'):
            replay = self.replays.load_replay(config['from_replay'])
            turn = max(0, min(int(config.get('from_turn') or 0), len(replay['frames'])))
            board = dict(replay['map'])
            board.setdefault('stage', replay.get('stage', 'prelim'))
            start_from = replay, turn
        else:
            try:
                board = self.get_map(config.get('map') or 'random', config.get('stage') or 'prelim')
            except KeyError as error:
                raise ValueError(error.args[0]) from error
        live = self._register_match(config, board, players, start_from=start_from)
        threading.Thread(target=live.run, daemon=True).start()
        return live

    def get_match(self, match_id):
        with self._lock:
            return self._matches[match_id]

    def match_list(self):
        with self._lock:
            matches = list(self._matches.values())
        return [live.status() for live in sorted(matches, key=lambda live: -live.created)]

    def start_tournament(self, config):
        bots = config['bots']
        if len(bots) < 2:
            raise ValueError('至少需要两个对战方')
        players = []
        for bot in bots:
            if bot.get('type') == 'human':
                raise ValueError('批量对战不支持人工操作')
            spec, name, cwd = self._player(bot)
            players.append((spec, bot.get('name') or name, cwd))

        stage = config.get('stage') or 'prelim'
        map_ids = config.get('maps') or random_map_ids(int(config.get('count') or 3), config.get('seed'))
        boards = resolve_boards(map_ids, self.maps, stage)

        def make_match(job, tournament_id):
            match_config = {'map': job.map_id, 'players': [bots[job.p0], bots[job.p1]],
                            'timing': config.get('timing', True), 'stage': stage}
            return self._register_match(match_config, boards[job.map_id], [players[job.p0], players[job.p1]],
                                        tournament=tournament_id)

        tournament = LiveTournament([player[1] for player in players], map_ids, make_match)
        with self._lock:
            if self._closed:
                raise ValueError('练习服务正在关闭')
            self._tournaments[tournament.id] = tournament
        threading.Thread(target=tournament.run, daemon=True).start()
        return tournament

    def get_tournament(self, tournament_id):
        with self._lock:
            return self._tournaments[tournament_id]

    def tournament_list(self):
        with self._lock:
            tournaments = list(self._tournaments.values())
        return [tournament.status() for tournament in tournaments]

    def close(self):
        with self._lock:
            self._closed = True
            tournaments = list(self._tournaments.values())
            matches = list(self._matches.values())
        for tournament in tournaments:
            tournament.stop()
        for live in matches:
            live.control('stop')
