"""Exercise the public package commands against real files and persisted state."""
import json

from .helpers import TEAMS, WorkspaceFixture, case, run_module, run_query_payload

from engine.mapgen import validate
from service.server import Contest


class EntrypointTests(WorkspaceFixture):
    @case('L1-009', '由双方和时间找回提交响应丢失的天梯编号')
    def test_ladder_submission_lookup_uses_original_side_order_and_acceptance_time(self):
        db = self.root / 'submissions.sqlite'
        contest = Contest(db)
        for sides in (TEAMS[::-1], ['team001', 'bot003']):
            contest.duels.accept({'sides': sides})
        expected = [contest.duels.accept({'sides': TEAMS})['duel_id'] for _ in range(2)]
        completed = run_module('service', '--db', db, 'ladder-submissions', *TEAMS,
                               '--since', '1970-01-01T00:00:00+08:00')
        duels = json.loads(completed.stdout)['duels']
        self.assertEqual([duel['duel_id'] for duel in duels], expected)
        self.assertEqual([duel['sides'] for duel in duels], [TEAMS] * 2)
        self.assertTrue(all(duel['accepted_at'].endswith('+08:00') for duel in duels))
        self.assertEqual(contest.duels.ladder_submissions(TEAMS, '9999-12-31T00:00:00+08:00'), {'duels': []})

    @case('L1-001', '地图批量导出参数和含空格路径')
    def test_mapgen_exports_both_stages_to_a_path_with_spaces(self):
        for stage in ('prelim', 'final'):
            for count in (1, 2):
                with self.subTest(stage=stage, count=count):
                    output = self.root / f'{stage} maps.json'
                    run_module('engine', 'mapgen', '--seed', 42, '--count', count,
                               '--stage', stage, '--out', output)
                    maps = json.loads(output.read_bytes())
                    self.assertIsInstance(maps, list)
                    self.assertEqual([board['id'] for board in maps], ['R42', 'R43'][:count])
                    self.assertEqual([board['stage'] for board in maps], [stage] * count)
                    for board in maps:
                        self.assertEqual(validate(board), [])

    @case('L1-002', '命令行管理操作与持久化状态一致')
    def test_management_commands_recover_persisted_executions(self):
        db = self.root / 'contest state.sqlite'
        contest = Contest(db)
        duel_id = contest.duels.accept({'sides': TEAMS})['duel_id']
        first = contest.runs.claim()
        run_id = first['run_id']
        contest.runs.complete_submission(run_id, None)

        run_module('service', '--db', db, 'resend', run_id)
        self.assertEqual(contest.duels.show(duel_id)['attempts'][0]['submission'], 'resend')
        self.assertEqual(contest.runs.claim(), first)
        contest.runs.complete_submission(run_id, 409)
        rejected = contest.duels.show(duel_id)['attempts'][0]
        self.assertEqual(rejected['submission'], 'rejected')
        self.assertIn('HTTP 409', rejected['issue'])
        self.assertIsNone(contest.runs.claim_poll())
        run_module('service', '--db', db, 'resend', run_id)
        self.assertEqual(contest.runs.claim(), first)
        contest.runs.complete_submission(run_id, 202)
        run_module('service', '--db', db, 'void', run_id, '主办方确认的平台故障')
        self.assertEqual(contest.duels.show(duel_id)['attempts'][0]['review'], 'void')

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

        completed = run_module('service', '--db', db, 'show', duel_id)
        recorded = json.loads(completed.stdout)
        self.assertEqual(recorded, contest.duels.show(duel_id))
        old, current, pending = recorded['attempts']
        self.assertEqual((old['run_id'], old['review'], old['issue']),
                         (run_id, 'void', '主办方确认的平台故障'))
        self.assertEqual((current['run_id'], current['review'], current['issue']), (replacement, None, None))
        self.assertEqual(pending['run_id'], 'run-2.1')
        rejected = run_module('service', '--db', db, 'retry', run_id, expected=1)
        self.assertIn('已被替代', rejected.stderr)
        self.assertNotIn('Traceback', rejected.stderr)
        self.assertEqual(contest.duels.show(duel_id), recorded)

