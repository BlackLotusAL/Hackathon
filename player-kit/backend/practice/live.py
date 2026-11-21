"""Interactive match sessions, human input and live event history."""
import json
import re
import threading
import time
import traceback

from .match import Match
from .replay_store import new_run_directory


class HumanBot:
    kind = 'human'
    name = '人工操作'

    def __init__(self):
        self.tick_ms = 400
        self.info = {'ready': True, 'name': 'human', 'sdk': 'web'}
        self._pending = None
        self._input_lock = threading.Lock()
        self._stopped = threading.Event()

    def press(self, cmd):
        if not isinstance(cmd, str) or not re.fullmatch(r'[UDLR]\+?', cmd):
            raise ValueError('命令应为 U/D/L/R，可带 +')
        with self._input_lock:
            self._pending = cmd

    def exited(self):
        return False

    def start(self, limit_s):
        return True, 'ready'

    def post(self, mid, what, msg, limit_s):
        return 'ok', 204, None, 0

    def turn(self, mid, msg, limit_s):
        started = time.perf_counter()
        self._stopped.wait(self.tick_ms / 1000)
        with self._input_lock:
            cmd, self._pending = self._pending, None
        elapsed = int((time.perf_counter() - started) * 1e6)
        reply = json.dumps({'turn': msg['turn'], 'cmd': cmd}) if cmd else '(沿原方向)'
        return cmd, 'ok', reply, elapsed

    def close(self):
        self._stopped.set()


class LiveMatch:
    def __init__(self, config, board, players, data_dir, *, start_from=None, tournament=None):
        specs, names, cwds = zip(*players)
        self.names = list(names)
        self.humans = {seat: bot for seat, bot in enumerate(specs) if isinstance(bot, HumanBot)}
        for bot in self.humans.values():
            bot.tick_ms = int(config.get('tick_ms') or 400)

        self.config = config
        self.tournament = tournament
        self.created = time.time()
        self.paused = bool(config.get('paused'))
        self.pace_ms = int(config.get('pace_ms') or 0)
        self.timing = bool(config.get('timing', True)) and not self.humans
        self.debug = bool(config.get('debug'))
        self.turn = 0
        self.result = None
        self.error = None
        self._condition = threading.Condition()
        self._events = []
        self._turns = {}
        self._steps = 0
        self._header_sent = False
        self._finished = False
        self.directory = new_run_directory(data_dir)
        self.id = self.directory.name
        self.match = Match(board, specs, out_dir=self.directory, names=self.names, timing=self.timing,
                           cwd=cwds, on_frame=self._on_frame, gate=self._gate,
                           keep_requests=self.debug, start_from=start_from,
                           max_turns=config.get('max_turns'), stage=config.get('stage'))

    def _emit(self, event):
        self._events.append(event)
        self._condition.notify_all()

    def _send_header(self):
        with self._condition:
            if self._header_sent or self.match.recorder is None:
                return
            self._header_sent = True
            data = self.match.recorder.data
            self._emit({
                'ev': 'header', 'id': self.id, 'map': data['map'], 'stage': data['stage'],
                'players': data['players'], 'params': data['params'], 'init': data['init'],
                'timing': self.timing, 'debug': self.debug, 'humans': sorted(self.humans),
                'bots': [getattr(bot, 'info', {}) if bot else {} for bot in self.match.bots],
            })
            for frame in data['frames']:
                self._emit({'ev': 'frame', 'frame': frame})

    def _gate(self, turn):
        self._send_header()
        with self._condition:
            self._condition.wait_for(lambda: not self.paused or self._steps > 0 or self.match.stopped)
            if self.paused and self._steps > 0:
                self._steps -= 1
            pace_ms = self.pace_ms
        if pace_ms and not self.humans and not self.match.stopped:
            time.sleep(pace_ms / 1000)

    def _on_frame(self, frame, entry):
        with self._condition:
            self.turn = entry['turn']
            lite = {key: entry[key] for key in ('turn', 'status', 'us', 'errors', 'cmds')}
            if self.debug:
                self._turns[self.turn] = {
                    'request': entry.get('request'), 'raw': entry['raw'], 'status': entry['status'],
                    'us': entry['us'], 'cmds': entry['cmds'],
                }
            else:
                lite['raw'] = entry['raw']
            if frame:
                self._emit({'ev': 'frame', 'frame': frame, 'entry': lite})
            else:
                self._emit({'ev': 'forfeit', 'entry': lite, 'reason': entry.get('reason')})

    def control(self, action, ms=None):
        with self._condition:
            if action == 'pause':
                self.paused = True
            elif action == 'resume':
                self.paused, self._steps = False, 0
            elif action == 'step':
                self.paused = True
                self._steps += 1
            elif action == 'stop':
                self.match.stop()
                for bot in self.humans.values():
                    bot.close()
            elif action == 'pace':
                self.pace_ms = max(0, min(2000, int(ms or 0)))
            elif action == 'tick':
                for bot in self.humans.values():
                    bot.tick_ms = max(100, min(3000, int(ms or 400)))
            else:
                raise ValueError('未知操作：%r' % action)
            status = self.status()
            if not self._finished:
                self._emit({'ev': 'status', 'status': status})
            return status

    def press(self, seat, cmd):
        if seat not in self.humans:
            raise ValueError(f'{seat} 号方不是人工操作')
        self.humans[seat].press(cmd)

    def debug_turn(self, turn):
        with self._condition:
            if turn not in self._turns:
                raise KeyError(f'第 {turn} 回合的调试记录（只有调试模式的对局保存请求内容）')
            return self._turns[turn]

    def event_snapshot(self):
        with self._condition:
            return list(self._events), self.status()

    def events_after(self, offset, timeout):
        with self._condition:
            self._condition.wait_for(lambda: len(self._events) > offset or self._finished, timeout)
            return self._events[offset:]

    def run(self):
        result, error = None, None
        try:
            metadata = {'id': self.id, 'cfg': self.config, 'names': self.names,
                        'created': self.created, 'tournament': self.tournament}
            (self.directory / 'meta.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
            result = self.match.run()
        except Exception as exception:
            error = f'{type(exception).__name__}: {exception}'
            traceback.print_exc()
        self._send_header()
        with self._condition:
            self.result, self.error = result, error
            self._finished = True
            self._emit({'ev': 'end', 'result': result, 'error': error})

    def status(self):
        with self._condition:
            state = 'error' if self.error else ('done' if self.result else self.match.status)
            return {'id': self.id, 'status': state, 'paused': self.paused, 'turn': self.turn, 'names': self.names,
                    'map': self.match.board['id'], 'result': self.result, 'error': self.error,
                    'pace_ms': self.pace_ms, 'humans': sorted(self.humans), 'timing': self.timing,
                    'debug': self.debug, 'created': self.created, 'tournament': self.tournament,
                    'errors': list(self.match.errors)}
