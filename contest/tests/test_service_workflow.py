"""Protect persistence and concurrency contracts across the Contest feature boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from threading import Barrier

from .helpers import (ArenaStub, TEAMS, WorkspaceFixture, case, plan_duel, roster_workbook,
                      run_query_payload, sealed_maps)

from service.domain import ContestError
from service.server import Contest, ContestWorker


class ServiceWorkflowTests(WorkspaceFixture):
    @case('L0-031', '跨赛程受理须等待旧赛程结算，作废待重跑时不下发下一赛程')
    def test_phase_switch_drains_unsettled_duels_across_restart(self):
        path = self.root / 'phases.sqlite'
        ladder = Contest(path)
        duel_id = ladder.duels.accept({'sides': TEAMS})['duel_id']
        config = {'prelim': sealed_maps('prelim')}
        prelim = Contest(path, config, phase='prelim')
        prelim.preliminary.import_roster(roster_workbook([(team, '') for team in TEAMS]))
        with self.assertRaises(ContestError) as busy:
            prelim.preliminary.plan()
        self.assertEqual(busy.exception.code, 'PHASE_BUSY')
        with self.assertRaises(ContestError) as inactive:
            prelim.duels.accept({'sides': TEAMS})
        self.assertEqual(inactive.exception.code, 'PHASE_INACTIVE')
        original = prelim.runs.claim()
        self.assertEqual(original['duel_id'], duel_id)
        prelim.runs.complete_submission(original['run_id'], 202)
        poll = prelim.runs.claim_poll()
        failure = {'run_id': poll.run_id, 'state': 'platform_error', 'reason': 'wall_timeout'}
        prelim.runs.complete_polls((poll,), 200, run_query_payload(failure))
        self.assertTrue(ContestWorker(prelim, self.root, ArenaStub()).review_next())

        # A second phase may already be queued in databases from earlier versions.
        plan_duel(prelim, 'duel-legacy-prelim', 'prelim')
        self.assertEqual(prelim.duels.status(duel_id)['state'], 'in_progress')
        sibling = prelim.runs.claim()
        self.assertEqual(sibling['duel_id'], duel_id)
        self.assertNotEqual(sibling['run_id'], original['run_id'])
        self.assertIsNone(prelim.runs.claim())
        replacement = prelim.runs.retry(original['run_id'])
        self.assertEqual(prelim.runs.claim(), dict(original, run_id=replacement))
        prelim.runs.complete_submission(replacement, 202)
        poll = prelim.runs.claim_poll()
        prelim.runs.complete_polls((poll,), 200, run_query_payload({'run_id': replacement, 'state': 'build_failed'}))
        evidence = {'bots': [{'team_id': team, 'state': 'failed', 'commit': '1' * 40} for team in original['bots']],
                    'duel_started_at': '2026-11-27T14:05:00+08:00'}
        prelim.runs.complete_review(replacement, 'build_failed', manifest=evidence)
        self.assertEqual(prelim.duels.status(duel_id)['state'], 'finished')
        self.assertEqual(prelim.preliminary.plan()['duels']['total'], 1)
        self.assertEqual(prelim.runs.claim()['duel_id'], 'duel-legacy-prelim')

    @case('L0-012', '计划创建失败时对决、预定局和尝试整体回滚')
    def test_partial_plan_failure_rolls_back_duels_games_and_attempts(self):
        contest = Contest(self.root / 'rollback.sqlite',
                          {'prelim': [{'seed': 42}, {'seed': float('nan')}, {'seed': 44}]}, phase='prelim')
        contest.preliminary.import_roster(roster_workbook([(team, '') for team in TEAMS]))
        with self.assertRaises(ValueError):
            contest.preliminary.plan()
        with closing(sqlite3.connect(contest.database.path)) as connection:
            for table in ('duels', 'games', 'runs'):
                self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        self.assertEqual(contest.preliminary.progress()['state'], 'roster_imported')
        contest.maps['prelim'] = sealed_maps('prelim')
        self.assertEqual(contest.preliminary.plan()['duels']['total'], 1)
        request = contest.runs.claim()
        self.assertEqual(request['run_id'], 'run-1.1')
        self.assertEqual(len(contest.duels.show(request['duel_id'])['attempts']), 6)

    @case('L0-013', '初赛和决赛规则的天梯对决均可并发领取不同执行')
    def test_concurrent_workers_claim_distinct_runs_for_both_ladder_phases(self):
        for phase in ('prelim-ladder', 'final-ladder'):
            with self.subTest(phase=phase):
                path = self.root / f'{phase}.sqlite'
                contest = Contest(path, phase=phase)
                duels = [contest.duels.accept({'sides': TEAMS})['duel_id'] for _ in range(2)]
                workers = [Contest(path), Contest(path)]
                gate = Barrier(2)

                def claim(worker):
                    gate.wait(timeout=5)
                    return worker.runs.claim()

                with ThreadPoolExecutor(2) as executor:
                    claimed = [run for run in executor.map(claim, workers) if run is not None]
                self.assertCountEqual([run['duel_id'] for run in claimed], duels)
                self.assertEqual(len({run['run_id'] for run in claimed}), len(duels))
