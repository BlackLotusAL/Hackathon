import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch

from engine.maps import generate
from engine.match import play_match
from engine.rules import PARAMS


class ScriptedBot:
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
        return command, self.status, self.elapsed_us

    def exited(self):
        return False

    def close(self):
        self.closed = True


class MatchTests(unittest.TestCase):
    def record_match(self, bots):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'replays' / 'match.json'
            with patch('engine.match.ProcessBot', side_effect=bots) as process_bot:
                game, errors = play_match(generate(42), 'submission', 'bot002', replay_path=path)
            self.assertEqual(process_bot.call_args_list, [call('submission'), call('bot002')])
            self.assertEqual(list(path.parent.iterdir()), [path])
            return game, errors, json.loads(path.read_text(encoding='utf-8'))

    def test_completed_match_uses_integer_response_times_to_break_ties(self):
        bots = [ScriptedBot('URDL', 1001), ScriptedBot('DLUR', 1004)]
        game, errors, replay = self.record_match(bots)

        self.assertEqual(game.turn, 600)
        self.assertEqual(game.metrics()['score'], [9, 9])
        self.assertEqual(game.time_us, [600600, 602400])
        self.assertEqual((game.winner, game.reason, errors), (0, 'time', [0, 0]))
        self.assertEqual(replay['format'], 'match-replay/2')
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
        self.assertEqual(replay['observations'][-1], {'phase': 'end', 'reason': 'errors'})
        self.assertTrue(all(bot.closed for bot in bots))
