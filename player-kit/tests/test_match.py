import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch

from engine import VERSION
from engine.game import Game
from engine.maps import generate
from engine.match import Match, play_match
from engine.protocol import init_message, turn_message
from engine.replay import game_result, read_referee_log, verify_replay, write_replay_index
from engine.rules import PARAMS
from sdk import InitState, TurnState


class ScriptedBot:
    exit_info = None

    def __init__(self, commands, elapsed_us, status='ok'):
        self.commands = commands
        self.elapsed_us = elapsed_us
        self.status = status
        self.notices = []
        self.run_ids = set()
        self.closed = False

    def start(self, limit):
        return True, 'ready'

    def post(self, run_id, phase, message, limit):
        self.notices.append(phase)
        self.run_ids.add(run_id)
        return 'ok', 204, None, 0

    def turn(self, run_id, message, limit):
        self.run_ids.add(run_id)
        index = (message['turn'] - 1) % len(self.commands)
        command = self.commands[index] if self.status == 'ok' else None
        return command, self.status, None, self.elapsed_us

    def exited(self):
        return False

    def close(self):
        self.closed = True


class MatchTests(unittest.TestCase):
    def test_strategy_shares_the_initialized_map_with_each_simulation(self):
        game = Game(generate(104))
        opening = init_message(game, 0)
        self.assertEqual(set(opening), {'type', 'seat', 'max_turns', 'map'})
        init = InitState.from_message(opening)
        first = TurnState.from_message(init, turn_message(game))
        game.step(('R', 'L'))
        second = TurnState.from_message(init, turn_message(game))
        self.assertIs(first.map, init.map)
        self.assertIs(second.game.map, init.map)
        self.assertIs(second.game.copy().map, init.map)
        self.assertEqual(second.game.turn, game.turn)
        self.assertEqual(second.game.trail, game.trail)
        self.assertEqual(second.map.to_payload(), opening['map'])

    def record_match(self, bots):
        board = generate(42)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'replays' / 'match.json'
            with patch('engine.match.ProcessBot', side_effect=bots) as process_bot:
                game, errors = play_match(board, 'submission', 'bot002', replay_path=path)
            self.assertEqual(process_bot.call_args_list, [call('submission'), call('bot002')])
            self.assertEqual(list(path.parent.iterdir()), [path])
            replay = json.loads(path.read_text(encoding='utf-8'))

            referee_bots = [ScriptedBot(bot.commands, bot.elapsed_us, bot.status) for bot in bots]
            output = root / 'referee'
            result = Match(board, referee_bots, names=['submission', 'bot002'],
                           run_id='run-42.1', out_dir=output).run()
            official = json.loads((output / 'replay.json').read_text(encoding='utf-8'))
            log = read_referee_log(output / 'log.jsonl')
            recomputed = verify_replay(official, board=board, log=log)

            for key in ('version', 'stage', 'map', 'players', 'params', 'init', 'observations'):
                self.assertEqual(replay[key], official[key])
            self.assertEqual(len(replay['frames']), len(official['frames']))
            for local_frame, official_frame in zip(replay['frames'], official['frames']):
                self.assertEqual(local_frame, official_frame, f'第 {official_frame["turn"]} 回合')
            self.assertEqual(game_result(game), game_result(recomputed))
            self.assertEqual(game_result(verify_replay(replay)), game_result(recomputed))
            self.assertEqual(replay['result'], dict(official['result'], errors=result['errors']))
            self.assertEqual(errors, result['errors'])
            return game, errors, replay

    def test_completed_match_uses_integer_response_times_to_break_ties(self):
        bots = [ScriptedBot('URDL', 1001), ScriptedBot('DLUR', 1004)]
        game, errors, replay = self.record_match(bots)

        self.assertEqual(game.turn, 600)
        self.assertEqual(game.metrics()['score'], [9, 9])
        self.assertEqual(game.time_us, [600600, 602400])
        self.assertEqual((game.winner, game.reason, errors), (0, 'time', [0, 0]))
        self.assertEqual(replay['version'], VERSION)
        self.assertFalse({'format', 'engine_version', 'source'} & replay.keys())
        self.assertEqual(replay['players'], ['submission', 'bot002'])
        self.assertEqual(len(replay['frames']), 600)
        self.assertEqual(replay['result']['time_us'], game.time_us)
        owner, contour = list(replay['init']['owner']), list(replay['init']['contour'])
        for frame in replay['frames']:
            for index, new_owner, new_contour in frame['d']:
                owner[index], contour[index] = new_owner, new_contour
        self.assertEqual(owner, ['.' if seat < 0 else str(seat) for seat in game.owner])
        self.assertEqual(contour, ['.' if seat < 0 else str(seat) for seat in game.trail])
        run_id, = bots[0].run_ids
        self.assertRegex(run_id, r'^run-[0-9]+\.1$')
        self.assertEqual(bots[1].run_ids, {run_id})
        for bot in bots:
            self.assertEqual(bot.notices, ['init', 'end'])
            self.assertTrue(bot.closed)

    def test_frames_with_territory_changes_match_the_referee(self):
        bots = [ScriptedBot('RRRRUUUULLLLDDDD', 1301), ScriptedBot('D', 1302)]
        game, errors, replay = self.record_match(bots)

        self.assertEqual((game.turn, errors), (600, [0, 0]))
        self.assertTrue(any(frame['d'] for frame in replay['frames']))

    def test_forfeit_counts_both_requests_without_settling_the_final_turn(self):
        bots = [ScriptedBot('U', 220000, status='timeout'), ScriptedBot('D', 1003)]
        game, errors, replay = self.record_match(bots)

        self.assertEqual((game.turn, game.winner, game.reason), (9, 1, 'errors'))
        self.assertEqual(errors, [10, 0])
        self.assertEqual(game.time_us, [10 * PARAMS['turn_ms'] * 1000, 10 * 1003])
        self.assertEqual(len(replay['frames']), 9)
        self.assertEqual(replay['frames'][0]['cmds'], ['', 'D'])
        self.assertEqual(replay['result']['errors'], errors)
        self.assertEqual(replay['observations'][-2]['turn'], 10)
        self.assertEqual(replay['observations'][-2]['us'], [PARAMS['turn_ms'] * 1000, 1003])
        self.assertEqual(replay['observations'][-1], {'phase': 'end', 'reason': 'errors', 'exits': [False, False]})
        self.assertTrue(all(bot.closed for bot in bots))

    def test_simultaneous_process_exit_stops_before_settling_the_request(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'bot.json').write_text('{"language":"python"}', encoding='utf-8')
            (root / 'README.md').write_text('Exit on turn three.', encoding='utf-8')
            (root / 'requirements.txt').touch()
            (root / 'strategy.py').write_text(
                'import os\nclass Strategy:\n'
                '    def __init__(self, state): pass\n'
                '    def act(self, state):\n'
                '        if state.turn == 3: os._exit(1)\n'
                '        return state.me.dir\n', encoding='utf-8')
            path = root / 'replays' / 'match.json'
            game, _ = play_match(generate(42), root, root, replay_path=path)
            self.assertEqual((game.turn, game.reason, game.winner), (2, 'double_loss', None))
            replay = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(replay['observations'][-2]['exits'], [True, True])

    def test_replay_index_only_lists_current_version(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            current = {'version': VERSION, 'stage': 'prelim', 'params': PARAMS, 'map': generate(42)}
            for filename, data in (('current.json', current),
                                   ('unknown.json', dict(current, version='v2')),
                                   ('retired.json', dict(current, stage='final')),
                                   ('rotating.json', dict(current, map={'boost_points': []})),
                                   ('unversioned.json', {}),
                                   ('invalid-params.json', dict(current, params=None))):
                (directory / filename).write_text(json.dumps(data), encoding='utf-8')
            write_replay_index(directory)
            listing = (directory / 'index.js').read_text(encoding='utf-8').partition(' = ')[2]
            self.assertEqual([name for name, _ in json.loads(listing.rstrip(';\n'))], ['current.json'])
