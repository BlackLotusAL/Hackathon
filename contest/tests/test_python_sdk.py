"""Exercise the Python SDK and baseline strategies against the official referee."""
from contextlib import ExitStack, contextmanager
import json
import os
import re
import subprocess
import sys

from engine.core import Game
from engine.protocol import dumps, init_message, turn_message
from judge.task import run_task

from .helpers import CONTEST, WorkspaceFixture, case, wait_for, write_judge_task


PLAYER_KIT = CONTEST.parent / 'player-kit'
REFEREE_ENGINE = CONTEST / 'engine/core.py'

# Supply the final simulator through the SDK's existing imports in the test process.
FINAL_ENGINE_SETUP = '''
import sys
from importlib.util import module_from_spec, spec_from_file_location

spec = spec_from_file_location('engine.game', sys.argv.pop(1))
engine = module_from_spec(spec)
sys.modules['engine.game'] = engine
spec.loader.exec_module(engine)
sys.modules['engine.rules'] = engine
'''


@contextmanager
def running_bot(rules, submission, log_path):
    environment = dict(os.environ, BOT_HOST='127.0.0.1')
    environment.pop('ARENA_NO_BUDGET', None)
    entry = ['-m', 'sdk.python']
    if rules == 'final':
        source = FINAL_ENGINE_SETUP + '\nfrom sdk.python.__main__ import main\nmain()\n'
        entry = ['-c', source, str(REFEREE_ENGINE)]
    with log_path.open('wb') as log:
        process = subprocess.Popen(
            [sys.executable, '-E', '-s', '-B', '-u', '-X', 'utf8', *entry, str(submission), '--port', '0'],
            cwd=PLAYER_KIT, env=environment, stdout=log, stderr=log,
        )

    def ready():
        messages = log_path.read_text(encoding='utf-8')
        if process.poll() is not None:
            raise AssertionError(messages)
        return re.search(r'^\[sdk\] listening on (127\.0\.0\.1:[0-9]+)$', messages, re.MULTILINE)

    try:
        address = wait_for(ready, bool, timeout=15)
        yield {'name': submission.name, 'url': 'http://' + address[1]}
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class PythonSDKTests(WorkspaceFixture):
    @case('L0-032', '同一份 Python 基准策略在初赛和决赛规则下打满 600 回合')
    def test_baseline_submissions_play_both_rule_sets(self):
        teams = CONTEST.parent / 'teams'
        submissions = [teams / name for name in ('bot002', 'bot003')]
        for number, rules in enumerate(('prelim', 'final'), 1):
            with self.subTest(rules=rules), ExitStack() as processes:
                logs = [self.root / f'{rules}-{submission.name}.log' for submission in submissions]
                bots = [processes.enter_context(running_bot(rules, submission, log))
                        for submission, log in zip(submissions, logs)]
                task = write_judge_task(self.root / rules, {
                    'run_id': f'run-{number}.1', 'rules': rules, 'bots': bots, 'config': {'map': {'seed': 42}},
                })
                result = run_task(task, self.root / f'out-{rules}')
                self.assertEqual((result['stage'], result['turns'], result['errors']), (rules, 600, [0, 0]),
                                 '\n'.join(log.read_text(encoding='utf-8') for log in logs))

    @case('L1-011', '决赛模拟保留同一倒计时下不同潜像的归属和整块作废行为')
    def test_final_snapshot_restores_latent_regions_for_simulation(self):
        game = Game({'id': 'test', 'size': 9, 'terrain': ['.' * 9] * 9,
                     'calib': [[2, 2], [6, 6]], 'stage': 'final'})
        game.beams[0].y = 4
        game.trail[game.idx(2, 4)] = 0
        for region, coordinates in ((4, ((3, 4), (5, 4))), (7, ((4, 4),))):
            game.regions[region] = [1, 3]
            for x, y in coordinates:
                cell = game.idx(x, y)
                game.owner[cell], game.lat[cell] = 1, region
        game.next_region = 8
        commands = ['R', 'D']
        snapshot = turn_message(game)
        source = FINAL_ENGINE_SETUP + '''
import json
from sdk import InitState, TurnState

opening, snapshot, commands = json.load(sys.stdin)
state = TurnState.from_message(InitState.from_message(opening), opening, snapshot)
game = state.game.copy()
events = game.step(commands)
print(json.dumps(dict(owner=game.owner, lat=game.lat, regions=game.regions, events=events,
                      countdowns=state.latent, original_regions=state.game.regions)))
'''
        simulated = subprocess.run(
            [sys.executable, '-E', '-s', '-B', '-c', source, str(REFEREE_ENGINE)], cwd=PLAYER_KIT,
            input=dumps((init_message(game, 0), snapshot, commands)), capture_output=True,
            encoding='utf-8', timeout=15, check=True,
        )
        original_regions = {str(region): values[:] for region, values in game.regions.items()}
        events = game.step(commands)
        self.assertIn({'t': 'void', 's': 0, 'n': 2, 'x': 3, 'y': 4}, events)
        self.assertEqual(json.loads(simulated.stdout), {
            'owner': game.owner, 'lat': game.lat, 'events': events, 'countdowns': snapshot['latent'],
            'regions': {str(region): values for region, values in game.regions.items()},
            'original_regions': original_regions,
        })
