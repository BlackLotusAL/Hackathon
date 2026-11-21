"""Protect persistence and concurrency contracts across the Contest feature boundaries."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import closing
import sqlite3
from threading import Barrier, Event
from unittest.mock import patch

from .helpers import ArenaStub, TEAMS, WorkspaceFixture, case, run_query_payload

from service.application import Contest, ContestWorker
from service.database import DatabaseStopping
from service.domain import ContestError



class ServiceWorkflowTests(WorkspaceFixture):
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
                          {'final': [{'seed': 42}, {'seed': float('nan')}, {'seed': 44}]})
        body = {'duel_id': 'duel-rollback', 'rules': 'final', 'sides': TEAMS}
        with self.assertRaises(ValueError):
            contest.duels.accept(body)
        with self.assertRaises(ContestError) as error:
            contest.duels.show(body['duel_id'])
        self.assertEqual(error.exception.code, 'DUEL_NOT_FOUND')
        with closing(sqlite3.connect(contest.database.path)) as connection:
            for table in ('duels', 'games', 'runs'):
                self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        contest.maps['final'] = [{'seed': seed} for seed in (42, 43, 44)]
        contest.duels.accept(body)
        self.assertEqual(contest.runs.claim()['run_id'], 'run-1.1')
        self.assertEqual(len(contest.duels.show(body['duel_id'])['attempts']), 6)

    @case('L0-013', '并发领取不能重复执行且决赛按场串行')
    def test_concurrent_workers_claim_distinct_runs_and_serialize_final_duels(self):
        for stage, expected in (('prelim', 2), ('final', 1)):
            with self.subTest(stage=stage):
                path = self.root / f'{stage}.sqlite'
                contest = Contest(path, {'final': [{'seed': seed} for seed in (42, 43, 44)]})
                for number in (1, 2):
                    body = {'duel_id': f'duel-{number}', 'rules': stage, 'sides': TEAMS}
                    if stage == 'prelim':
                        body['map_seed'] = 42
                    contest.duels.accept(body)
                workers = [Contest(path), Contest(path)]
                gate = Barrier(2)

                def claim(worker):
                    gate.wait(timeout=5)
                    return worker.runs.claim()

                with ThreadPoolExecutor(2) as executor:
                    claimed = [run for run in executor.map(claim, workers) if run is not None]
                self.assertEqual(len(claimed), expected)
                self.assertEqual(len({run['run_id'] for run in claimed}), expected)
                if stage == 'final':
                    self.assertEqual(claimed[0]['duel_id'], 'duel-1')
                    self.assertTrue(all(attempt['submission'] == 'pending'
                                        for attempt in contest.duels.show('duel-2')['attempts']))

    @case('L1-006', '丢失提交响应后按原编号找回状态，查询失败退避且不重复提交')
    def test_polling_recovers_uncertain_submissions_and_retries_read_failures(self):
        path = self.root / 'poll.sqlite'
        contest = Contest(path)
        duel_id = 'duel-uncertain'
        contest.duels.accept({'duel_id': duel_id, 'rules': 'prelim', 'sides': TEAMS, 'map_seed': 42})
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
            contest.duels.accept({'duel_id': 'duel-lock', 'rules': 'prelim', 'sides': TEAMS, 'map_seed': 42})
            with ThreadPoolExecutor(1) as executor, closing(sqlite3.connect(db)) as blocker:
                response = executor.submit(worker.submit_next)
                try:
                    self.assertTrue(executing.wait(5))
                    blocker.execute('BEGIN IMMEDIATE')
                    deliver_response.set()
                    with self.assertRaises(TimeoutError):
                        response.result(timeout=0.25)
                    self.assertEqual(contest.duels.show('duel-lock')['attempts'][0]['submission'], 'sending')
                finally:
                    deliver_response.set()
                    blocker.rollback()
                self.assertTrue(response.result(timeout=5))
                self.assertEqual(len(arena.submitted), 1)
                self.assertEqual(contest.duels.show('duel-lock')['attempts'][0]['submission'], 'accepted')
                blocker.execute('BEGIN IMMEDIATE')
                waiting = executor.submit(contest.runs.claim)
                shutdown.set()
                with self.assertRaises(DatabaseStopping):
                    waiting.result(timeout=5)
