"""Drive the Bot sessions and record judge observations and replays."""
import http.client
from pathlib import Path
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from .adjudication import TIME_LIMIT_STATES, apply_startup, apply_turn
from .core import Game
from .rules import PARAMS
from .protocol import (MAX_RESPONSE_BYTES, dumps, end_message, init_message,
                       json_object, parse_reply, turn_message)
from .replay import Recorder, public_observation


class HttpBot:
    """HTTP client for a Bot service whose lifecycle is managed by the caller."""

    exit_info = None

    def __init__(self, url):
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('Bot URL 必须是 HTTP(S) 服务根地址')
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if self.scheme == 'https' else 80)

    def exited(self):
        return False

    def request(self, method, path, body, limit):
        """Return transport status, HTTP status, body and elapsed microseconds."""
        if self.exited():
            return 'crash', 0, None, 0
        headers = {'Connection': 'close'}
        data = None
        if body is not None:
            data = dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        t0 = time.perf_counter()
        connection = http.client.HTTPSConnection if self.scheme == 'https' else http.client.HTTPConnection
        conn = connection(self.host, self.port, timeout=limit)
        deadline = None
        try:
            conn.connect()
            sock = conn.sock
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            def cancel():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            remaining = limit - (time.perf_counter() - t0)
            if remaining <= 0:
                raise socket.timeout()
            sock.settimeout(remaining)
            # Bound the whole exchange, including slow headers and trickled bodies.
            deadline = threading.Timer(remaining, cancel)
            deadline.daemon = True
            deadline.start()
            conn.request(method, path, body=data, headers=headers)
            with conn.getresponse() as response:
                out = response.read(MAX_RESPONSE_BYTES + 1)
                if response.length and len(out) <= MAX_RESPONSE_BYTES:
                    raise http.client.IncompleteRead(out, response.length)
                code = response.status
        except (OSError, http.client.HTTPException, ValueError) as exc:
            us = int((time.perf_counter() - t0) * 1e6)
            status = 'timeout' if isinstance(exc, socket.timeout) or us >= limit * 1e6 else 'connection'
            return status, 0, str(exc)[:200], us
        finally:
            if deadline is not None:
                deadline.cancel()
            conn.close()
        us = int((time.perf_counter() - t0) * 1e6)
        if us > limit * 1e6:
            return 'timeout', code, None, us
        if len(out) > MAX_RESPONSE_BYTES:
            return 'invalid', code, None, us
        return 'ok', code, out, us

    # ---------------------------------------------------------------- 协议
    def start(self, limit_s):
        """等待就绪。返回 (ok, 说明)。"""
        end = time.monotonic() + limit_s
        while time.monotonic() < end:
            if self.exited():
                return False, 'crash'
            st, code, data, _ = self.request('GET', '/v1/health', None, min(1.0, max(0.001, end - time.monotonic())))
            if st == 'ok' and code == 200:
                try:
                    obj = json_object(data)
                except (ValueError, RecursionError):
                    obj = None
                if isinstance(obj, dict) and obj.get('ready') is True:
                    return True, 'ready'
            elif st == 'ok' and code in (401, 403):
                return False, 'startup'               # Bot 已拒绝启动请求。
            time.sleep(min(0.05, max(0, end - time.monotonic())))
        return False, 'startup' if not self.exited() else 'crash'

    def post(self, run_id, what, msg, limit_s):
        st, code, data, us = self.request('POST', f'/v1/matches/{run_id}/{what}', msg, limit_s)
        if st == 'ok' and not 200 <= code < 300:
            st = 'invalid'
        return st, code, data, us

    def turn(self, run_id, msg, limit_s):
        """返回 (cmd, status, raw, us)，区分超时、连接失败、无效回复和请求前的程序退出。"""
        t = msg['turn']
        st, code, data, us = self.post(run_id, 'turns/%d' % t, msg, limit_s)
        raw = data.decode('utf-8', 'replace')[:200] if isinstance(data, bytes) else data
        if st == 'crash':
            return None, 'crash', raw, us
        if st == 'timeout':
            return None, 'timeout', raw, us
        if st != 'ok' or code != 200:
            return None, 'connection' if st == 'connection' else 'invalid', raw, us
        r = parse_reply(data)
        if r is None or r[0] != t:
            return None, 'invalid', raw, us
        return r[1], 'ok', raw, us


class Match:
    """Run a timed judge match with the Bot clients supplied by the task."""

    def __init__(self, board, bots, *, out_dir, names, run_id):
        self.board, self.bots, self.names = board, bots, names
        self.out_dir = Path(out_dir)
        self.run_id = run_id

    @staticmethod
    def _observe(log, recorder, entry):
        log.write(dumps(entry) + '\n')
        recorder.data['observations'].append(public_observation(entry))

    def run(self):
        game = Game(self.board)
        recorder = Recorder(game, self.names)
        errors = [0, 0]
        self.out_dir.mkdir(parents=True, exist_ok=True)

        with (self.out_dir / 'log.jsonl').open('w', encoding='utf-8') as log, ThreadPoolExecutor(2) as pool:
            starters = [pool.submit(bot.start, PARAMS['startup_s']) for bot in self.bots]
            ready = [future.result() for future in starters]
            startup = [state for _, state in ready]
            initial = [None, None]
            if all(ok for ok, _ in ready):
                futures = [
                    pool.submit(bot.post, self.run_id, 'init', init_message(game, seat), PARAMS['init_ms'] / 1000.0)
                    for seat, bot in enumerate(self.bots)
                ]
                initial = [future.result()[0] for future in futures]
            failures = apply_startup(game, errors, startup, initial, [bot.exited() for bot in self.bots])
            entry = {'phase': 'startup', 'turn': game.turn, 'startup': startup,
                     'init': initial, 'errors': list(errors), 'failures': failures,
                     'exits': [bot.exit_info for bot in self.bots]}
            if game.done:
                entry.update({'forfeit': game.winner, 'reason': game.reason})
            self._observe(log, recorder, entry)

            while not game.done:
                self._turn(game, recorder, pool, log, errors)

            message = end_message(game)
            pending = {
                seat: pool.submit(bot.post, self.run_id, 'end', message, PARAMS['end_ms'] / 1000.0)
                for seat, bot in enumerate(self.bots) if ready[seat][0] and not bot.exited()
            }
            ends = [pending[seat].result()[0] if seat in pending else None for seat in (0, 1)]
            self._observe(log, recorder, {'phase': 'end', 'turn': game.turn, 'end': ends,
                                          'winner': game.winner, 'reason': game.reason,
                                          'exits': [bot.exit_info for bot in self.bots]})

        replay = recorder.finish()
        (self.out_dir / 'replay.json').write_text(dumps(replay), encoding='utf-8')
        return dict(replay['result'], map=game.map.id, stage=game.map.stage, players=self.names,
                    errors=errors, run_id=self.run_id)

    def _turn(self, game, recorder, pool, log, errors):
        message = turn_message(game)
        limit = PARAMS['turn_ms'] / 1000.0
        futures = [pool.submit(bot.turn, self.run_id, message, limit) for bot in self.bots]
        responses = [future.result() for future in futures]
        commands, statuses, replies, elapsed = [list(field) for field in zip(*responses)]
        for seat, status in enumerate(statuses):
            if status in TIME_LIMIT_STATES:
                elapsed[seat] = PARAMS['turn_ms'] * 1000

        apply_turn(game, errors, statuses, elapsed, [bot.exited() for bot in self.bots])
        entry = {'turn': message['turn'], 'cmds': commands, 'status': statuses,
                 'us': elapsed, 'errors': list(errors), 'raw': replies}
        if game.done:
            entry.update({'forfeit': game.winner, 'reason': game.reason,
                          'exits': [bot.exit_info for bot in self.bots]})
        else:
            events = game.step(commands)
            recorder.record([command or '' for command in commands], events,
                            [round(us / 1000.0, 2) for us in elapsed])
            entry.update({'score': [game.score(0), game.score(1)], 'events': events})
        self._observe(log, recorder, entry)
