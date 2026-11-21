"""Protect persistence and concurrency contracts across the Contest feature boundaries."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import closing
import sqlite3
from threading import Barrier, Event
from unittest.mock import patch

from .helpers import ArenaStub, TEAMS, WorkspaceFixture, case, plan_duel, roster_workbook, run_query_payload, sealed_maps

from service.database import SCHEMA, DatabaseStopping
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

    @case('L1-010', '升级已有数据库，保留运行编号和历史查询并记录新受理时间')
    def test_version_two_database_migrates_existing_matches(self):
        path = self.root / 'version-two.sqlite'
        with closing(sqlite3.connect(path)) as connection, connection:
            for statement in SCHEMA:
                connection.execute(statement.replace(', accepted_at TEXT', ''))
            connection.execute('PRAGMA user_version = 2')
            connection.execute("""
                INSERT INTO duels (id, duel_id, submission, kind)
                VALUES (1, 'duel-legacy', '{"duel_id":"duel-legacy","rules":"prelim","sides":["team001","team002"]}',
                        'prelim-ladder')
            """)
            connection.execute("""INSERT INTO games VALUES (22, 1, 1, '["team001","team002"]', '{"seed":42}')""")
            connection.execute("INSERT INTO runs (run_id, game, attempt) VALUES ('run-22.1', 22, 1)")

        contest = Contest(path)
        self.assertEqual(contest.duels.status('duel-legacy')['state'], 'in_progress')
        self.assertEqual(contest.runs.claim()['run_id'], 'run-22.1')
        duel_id = contest.duels.accept({'sides': TEAMS})['duel_id']
        found = Contest(path).duels.ladder_submissions(TEAMS, '1970-01-01T00:00:00+08:00')['duels']
        self.assertEqual([duel['duel_id'] for duel in found], [duel_id])

    @case('L2-003', '拒绝不受支持的数据库结构并保留原文件')
    def test_unsupported_database_is_left_intact(self):
        path = self.root / 'other-schema.sqlite'
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('CREATE TABLE runs (run_id TEXT PRIMARY KEY)')
            connection.execute("INSERT INTO runs VALUES ('run-1.1')")
        original = path.read_bytes()

        with self.assertRaisesRegex(ValueError, '数据库结构版本不受支持'):
            Contest(path)
        self.assertEqual(path.read_bytes(), original)

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

    @case('L1-006', '丢失提交响应后按原编号找回状态，查询失败退避且不重复提交')
    def test_polling_recovers_uncertain_submissions_and_retries_read_failures(self):
        path = self.root / 'poll.sqlite'
        contest = Contest(path)
        duel_id = contest.duels.accept({'sides': TEAMS})['duel_id']
        request = contest.runs.claim()
        run_id = request['run_id']
        contest.runs.complete_submission(run_id, None)

        poll = contest.runs.claim_poll(now=100)
        self.assertEqual(poll.run_id, run_id)
        self.assertIsNone(contest.runs.claim_poll(now=100))
        contest.runs.complete_polls((poll,), 200, run_query_payload(missing=[run_id]), now=100)
        self.assertIsNone(contest.runs.claim_poll(now=100.5))
        self.assertEqual(contest.duels.show(duel_id)['attempts'][0]['submission'], 'unconfirmed')
        self.assertIsNone(contest.runs.claim())

        poll = contest.runs.claim_poll(now=101)
        contest.runs.complete_polls((poll,), 200, run_query_payload({'run_id': run_id, 'state': 'queued'}), now=101)
        self.assertEqual(contest.duels.show(duel_id)['attempts'][0]['submission'], 'accepted')
        self.assertIsNone(contest.runs.claim_poll(now=101.49))
        poll = contest.runs.claim_poll(now=101.5)
        contest.runs.complete_polls((poll,), 404, None, now=101.5)
        attempt = contest.duels.show(duel_id)['attempts'][0]
        self.assertEqual(attempt['issue'], 'Arena 执行查询失败（HTTP 404）')
        self.assertIsNone(attempt['arena_result'])

        self.assertIsNone(contest.runs.claim_poll(now=102))
        poll = contest.runs.claim_poll(now=103)
        contest.runs.complete_polls((poll,), 200, run_query_payload(missing=[run_id]), now=103)
        missing = contest.duels.show(duel_id)['attempts'][0]
        self.assertIn('未找到已受理', missing['issue'])
        self.assertIsNone(missing['arena_result'])

        self.assertIsNone(contest.runs.claim_poll(now=104))
        poll = contest.runs.claim_poll(now=105)
        contest.runs.complete_polls((poll,), 503, None, now=105)
        self.assertIsNone(contest.runs.claim_poll(now=108))
        contest = Contest(path)
        poll = contest.runs.claim_poll(now=109)
        contest.runs.complete_polls((poll,), 200, run_query_payload({'run_id': run_id, 'state': 'finished'}), now=109)
        first = contest.duels.show(duel_id)['attempts'][0]
        self.assertEqual((first['submission'], first['arena_result'], first['issue']),
                         ('accepted', {'state': 'finished'}, None))
        self.assertIsNone(contest.runs.claim())
        self.assertEqual(Contest(path).runs.next_review()[0], request)

    @case('L1-008', 'SQLite 写锁释放后保留已收到的 Arena 响应，服务停止时退出锁等待')
    def test_write_lock_wait_preserves_a_submission_response(self):
        db = self.root / 'contended.sqlite'
        shutdown = Event()
        executing, deliver_response = Event(), Event()
        arena = ArenaStub()
        original_submit = arena.submit

        def submit(request):
            status = original_submit(request)
            executing.set()
            if not deliver_response.wait(5):
                raise TimeoutError('Arena fixture did not receive permission to reply')
            return status

        with patch('service.database.SQLITE_BUSY_TIMEOUT', 0.05), patch.object(arena, 'submit', submit):
            contest = Contest(db, shutdown=shutdown)
            worker = ContestWorker(contest, self.root, arena)
            duel_id = contest.duels.accept({'sides': TEAMS})['duel_id']
            with ThreadPoolExecutor(1) as executor, closing(sqlite3.connect(db)) as blocker:
                response = executor.submit(worker.submit_next)
                try:
                    self.assertTrue(executing.wait(5))
                    blocker.execute('BEGIN IMMEDIATE')
                    deliver_response.set()
                    with self.assertRaises(TimeoutError):
                        response.result(timeout=0.25)
                    self.assertEqual(contest.duels.show(duel_id)['attempts'][0]['submission'], 'sending')
                finally:
                    deliver_response.set()
                    blocker.rollback()
                self.assertTrue(response.result(timeout=5))
                self.assertEqual(len(arena.submitted), 1)
                self.assertEqual(contest.duels.show(duel_id)['attempts'][0]['submission'], 'accepted')
                blocker.execute('BEGIN IMMEDIATE')
                waiting = executor.submit(contest.runs.claim)
                shutdown.set()
                with self.assertRaises(DatabaseStopping):
                    waiting.result(timeout=5)
