"""Run local matches and write their results, logs, and replays."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets

from game_core import mapgen
from game_core.adjudication import TIME_LIMIT_STATES, apply_startup, apply_turn
from game_core.protocol import dumps, end_message, init_message, turn_message
from game_core.replay import Recorder, public_observation, replay_to
from game_core.rules import ENGINE_VERSION, Game, PARAMS

from ..runtime.local import bot_name, make_bot
from .catalog import load_maps
from .replay_store import data_directory, new_run_directory, publish_replay


class Match:
    """运行一局本地对局，在回合开始和结算时向调用方报告进度。"""

    def __init__(self, board, specs, *, out_dir=None, names=None, timing=True, match_id=None, cwd=None,
                 on_frame=None, gate=None, keep_requests=False, start_from=None, stage=None, max_turns=None):
        self.board = board
        self.specs = specs
        self.work_dirs = cwd if isinstance(cwd, (list, tuple)) else (cwd, cwd)
        if names is None:
            names = [
                bot_name(spec, self.work_dirs[seat]) if isinstance(spec, str) else getattr(spec, 'name', 'bot')
                for seat, spec in enumerate(specs)
            ]
        self.names = names
        self.out_dir = Path(out_dir) if out_dir else None
        self.timing = timing
        self.mid = match_id or '%s-%s' % (board['id'], secrets.token_hex(4))
        self.on_frame, self.gate, self.keep_requests = on_frame, gate, keep_requests
        self.start_from = start_from
        self.stage = stage
        self.max_turns = max_turns
        self.record_observations = timing and start_from is None and max_turns is None
        self.stopped = False
        self.status = 'pending'
        self.game = None
        self.recorder = None
        self.errors = [0, 0]
        self.bots = [None, None]

    def stop(self):
        self.stopped = True

    def sid(self, seat):
        """发给 Bot 的 match_id：两个座位各用一个，同一个服务自己打自己也不会混。"""
        return '%s-%d' % (self.mid, seat)

    def _limit(self, key):
        return PARAMS[key] / 1000.0 if self.timing else 3600.0

    def _observe(self, log, entry):
        if log:
            log.write(dumps({key: value for key, value in entry.items() if key != 'request'}) + '\n')
        if self.record_observations:
            self.recorder.data['observations'].append(public_observation(entry))

    def run(self):
        if self.out_dir:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        if self.start_from:
            rp, upto = self.start_from
            g, pre = replay_to(self.board, rp, upto)
            g0 = Game(self.board, stage=rp.get('stage'))
        else:
            g = Game(self.board, stage=self.stage, max_turns=self.max_turns)
            g0, pre = g, []
        rec = Recorder(g0, self.names)
        if self.record_observations:
            rec.data.update(engine_version=ENGINE_VERSION, observations=[])
        if pre:                                        # 回放前段：在新回放里照抄
            tmp = Game(self.board, stage=g0.stage)
            rec.game = tmp
            for cmds, _, ms in pre:
                ev = tmp.step([c if c else None for c in cmds])
                rec.record(cmds, ev, ms)
            rec.game = g
        self.game, self.recorder = g, rec
        log = (self.out_dir / 'log.jsonl').open('w', encoding='utf-8') if self.out_dir else None
        errors = self.errors
        pool = None
        try:
            self.status = 'starting'
            for seat, spec in enumerate(self.specs):
                stderr_path = self.out_dir / f'p{seat}.stderr.log' if self.out_dir else None
                self.bots[seat] = make_bot(spec, cwd=self.work_dirs[seat], stderr_path=stderr_path)
            bots = self.bots
            pool = ThreadPoolExecutor(2)
            starters = [pool.submit(b.start, PARAMS['startup_s']) for b in bots]
            ready = [f.result() for f in starters]
            startup = [state for _, state in ready]
            initial = [None, None]
            if all(ok for ok, _ in ready):
                futures = [pool.submit(b.post, self.sid(s), 'init', init_message(g, s), self._limit('init_ms'))
                           for s, b in enumerate(bots)]
                initial = [f.result()[0] for f in futures]
            failures = apply_startup(g, errors, startup, initial, [b.exited() for b in bots])
            entry = {'phase': 'startup', 'turn': g.turn, 'startup': startup,
                     'init': initial, 'errors': list(errors),
                     'failures': failures, 'exits': [getattr(b, 'exit_info', None) for b in bots]}
            if g.done:
                entry.update({'forfeit': g.winner, 'reason': g.reason})
            self._observe(log, entry)
            if not g.done:
                self.status = 'running'
                round_pool = pool if all(b.kind == 'http' for b in bots) else None
                while not g.done and not self.stopped:
                    if self.gate:
                        self.gate(g.turn + 1)
                        if self.stopped:
                            break
                    self._turn(g, rec, bots, round_pool, log)
            if self.stopped and not g.done:
                g.forfeit(-1, 'stopped')
            data = rec.finish()
            msg = end_message(g)
            pending = {s: pool.submit(b.post, self.sid(s), 'end', msg, PARAMS['end_ms'] / 1000.0)
                       for s, b in enumerate(bots) if ready[s][0] and not b.exited()}
            ends = [pending[s].result()[0] if s in pending else None for s in (0, 1)]
            self._observe(log, {'phase': 'end', 'turn': g.turn, 'end': ends, 'winner': g.winner,
                                'reason': g.reason, 'exits': [getattr(b, 'exit_info', None) for b in bots]})
        finally:
            if pool:
                pool.shutdown(wait=False)
            for b in self.bots:
                if b is not None:
                    b.close()
            if log:
                log.close()
        res = dict(data['result'])
        res.update({'map': self.board['id'], 'stage': g.stage, 'players': self.names, 'errors': list(errors),
                    'match_id': self.mid})
        if self.out_dir:
            with (self.out_dir / 'result.json').open('w', encoding='utf-8') as f:
                json.dump(res, f, ensure_ascii=False, indent=1)
            publish_replay(self.out_dir / 'replay.json', data)
        self.status = 'done'
        self.result = res
        return res

    def _turn(self, g, rec, bots, pool, log):
        msg = turn_message(g)
        t = msg['turn']
        limit = self._limit('turn_ms')
        if pool:
            futs = [pool.submit(bots[s].turn, self.sid(s), msg, limit) for s in (0, 1)]
            outs = [f.result() for f in futs]
        else:
            outs = [bots[s].turn(self.sid(s), msg, limit) for s in (0, 1)]
        cmds = [o[0] for o in outs]
        stat = [o[1] for o in outs]
        raws = [o[2] for o in outs]
        us = [o[3] for o in outs]
        for s in (0, 1):
            if stat[s] in TIME_LIMIT_STATES and self.timing:
                us[s] = PARAMS['turn_ms'] * 1000
        apply_turn(g, self.errors, stat, us, [b.exited() for b in bots])
        entry = {'turn': t, 'cmds': cmds, 'status': stat, 'us': us, 'errors': list(self.errors), 'raw': raws}
        if self.keep_requests:
            entry['request'] = msg
        if g.done:
            entry.update({'forfeit': g.winner, 'reason': g.reason,
                          'exits': [getattr(b, 'exit_info', None) for b in bots]})
            self._observe(log, entry)
            if self.on_frame:
                self.on_frame(None, entry)
            return
        ev = g.step(cmds)
        ms = [round(u / 1000.0, 2) for u in us]
        rec.record([c if c else '' for c in cmds], ev, ms)
        entry.update({'score': [g.score(0), g.score(1)], 'events': ev})
        self._observe(log, entry)
        if self.on_frame:
            self.on_frame(rec.data['frames'][-1], entry)


def prepare_match(specs, *, map_name=None, output=None, data_dir=None, timing=True, on_frame=None):
    board = mapgen.get(map_name, maps=load_maps())
    directory = Path(output).expanduser().resolve() if output else new_run_directory(data_directory(data_dir))
    return Match(board, specs, out_dir=directory, timing=timing, on_frame=on_frame)
