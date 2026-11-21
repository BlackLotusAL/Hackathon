"""Exercise the public package commands against real files and persisted state."""
import json

from .helpers import TEAMS, WorkspaceFixture, case, run_module, run_query_payload

from engine.mapgen import validate
from service.application import Contest


class EntrypointTests(WorkspaceFixture):
    @case('L2-001', '包帮助信息可显示')
    def test_package_help(self):
        for package in ('engine', 'judge', 'service'):
            with self.subTest(package=package):
                completed = run_module(package, '--help')
                self.assertIn(f'python -m {package}', completed.stdout)
                self.assertEqual(completed.stderr, '')

    @case('L1-001', '地图批量导出参数和含空格路径')
    def test_mapgen_exports_both_stages_to_a_path_with_spaces(self):
        for stage in ('prelim', 'final'):
            with self.subTest(stage=stage):
                output = self.root / f'{stage} maps.json'
                run_module('engine', 'mapgen', '--seed', 42, '--count', 2,
                                '--stage', stage, '--out', output)
                maps = json.loads(output.read_text(encoding='utf-8'))
                self.assertEqual([board['id'] for board in maps], ['R42', 'R43'])
                self.assertEqual([board['stage'] for board in maps], [stage, stage])
                for board in maps:
                    self.assertEqual(validate(board), [])

    @case('L1-002', '命令行管理操作与持久化状态一致')
    def test_management_commands_recover_persisted_executions(self):
        db = self.root / 'contest state.sqlite'
        contest = Contest(db)
        contest.duels.accept({'duel_id': 'duel-1', 'rules': 'prelim', 'sides': TEAMS, 'map_seed': 42})
        first = contest.runs.claim()
        run_id = first['run_id']
        contest.runs.complete_submission(run_id, None)

        run_module('service', '--db', db, 'resend', run_id)
        self.assertEqual(contest.duels.show('duel-1')['attempts'][0]['submission'], 'resend')
        self.assertEqual(contest.runs.claim(), first)
        contest.runs.complete_submission(run_id, 409)
        rejected = contest.duels.show('duel-1')['attempts'][0]
        self.assertEqual(rejected['submission'], 'rejected')
        self.assertIn('HTTP 409', rejected['issue'])
        self.assertIsNone(contest.runs.claim_poll())
        run_module('service', '--db', db, 'resend', run_id)
        self.assertEqual(contest.runs.claim(), first)
        contest.runs.complete_submission(run_id, 202)
        run_module('service', '--db', db, 'void', run_id, '主办方确认的平台故障')
        self.assertEqual(contest.duels.show('duel-1')['attempts'][0]['review'], 'void')

        completed = run_module('service', '--db', db, 'retry', run_id)
        replacement = completed.stdout.strip()
        self.assertEqual(replacement, 'run-1.2')
        self.assertEqual(contest.runs.claim(), dict(first, run_id=replacement))
        contest.runs.complete_submission(replacement, 202)
        poll = contest.runs.claim_poll()
        self.assertEqual(poll.run_id, replacement)
        contest.runs.complete_polls((poll,), 200, run_query_payload({'run_id': replacement, 'state': 'finished'}))
        contest.runs.complete_review(replacement, 'held', issue='等待复核归档')
        run_module('service', '--db', db, 'recheck', replacement)
        request, outcome, _ = contest.runs.next_review()
        self.assertEqual(request, dict(first, run_id=replacement))
        self.assertEqual(outcome, {'state': 'finished'})

        completed = run_module('service', '--db', db, 'show', 'duel-1')
        recorded = json.loads(completed.stdout)
        self.assertEqual(recorded, contest.duels.show('duel-1'))
        old, current, pending = recorded['attempts']
        self.assertEqual((old['run_id'], old['review'], old['issue']),
                         (run_id, 'void', '主办方确认的平台故障'))
        self.assertEqual((current['run_id'], current['review'], current['issue']), (replacement, None, None))
        self.assertEqual(pending['run_id'], 'run-2.1')
        rejected = run_module('service', '--db', db, 'retry', run_id, expected=1)
        self.assertIn('已被替代', rejected.stderr)
        self.assertNotIn('Traceback', rejected.stderr)
        self.assertEqual(contest.duels.show('duel-1'), recorded)

