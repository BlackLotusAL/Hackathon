"""Protect persistence and concurrency contracts across the Contest feature boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3
from threading import Barrier

from .helpers import ArenaStub, HttpClient, TEAMS, WorkspaceFixture, case, contest_handler

from service.application import Contest, ContestWorker
from service.domain import ContestError


LEGACY_SCHEMA = '''
CREATE TABLE duels (
    id INTEGER PRIMARY KEY, duel_id TEXT UNIQUE NOT NULL, submission TEXT NOT NULL,
    kind TEXT NOT NULL, commits TEXT, started_at TEXT, issue TEXT, result TEXT,
    confirmed_order INTEGER UNIQUE, delivery TEXT
);
CREATE TABLE games (
    id INTEGER PRIMARY KEY AUTOINCREMENT, duel INTEGER NOT NULL REFERENCES duels(id),
    ordinal INTEGER NOT NULL, bots TEXT NOT NULL, map TEXT NOT NULL, UNIQUE (duel, ordinal)
);
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, game INTEGER NOT NULL REFERENCES games(id), attempt INTEGER NOT NULL,
    submission TEXT NOT NULL DEFAULT 'pending', callback TEXT,
    review TEXT, issue TEXT, manifest TEXT, UNIQUE (game, attempt)
);
'''


class ServiceWorkflowTests(WorkspaceFixture):
    @case('L2-003', '历史数据库保留原编号和 Arena 恢复能力，旧结算可查询')
    def test_preexisting_database_recovers_without_rewriting_legacy_plans(self):
        path = self.root / 'legacy.sqlite'
        sides = ['t001', 'bot:L2']
        submission = {'duel_id': 'duel-old-run', 'rules': 'prelim', 'sides': sides, 'map_seed': 42}
        result = {'outcome': 'win', 'winner': 't001', 'runs': ['run-2.1', 'run-3.1'],
                  'games_won': {'t001': 2, 'bot:L2': 0}, 'games_lost': {'t001': 0, 'bot:L2': 2},
                  'started_at': '2026-11-19T10:00:00+08:00', 'ended_at': '2026-11-19T10:05:00+08:00'}
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(LEGACY_SCHEMA)
            connection.execute('INSERT INTO duels (id, duel_id, submission, kind) VALUES (1, ?, ?, ?)',
                               ('duel-old-run', json.dumps(submission), 'prelim-ladder'))
            connection.execute('''INSERT INTO duels (id, duel_id, submission, kind, result, confirmed_order, delivery)
                                  VALUES (2, ?, ?, ?, ?, 1, 'sending')''',
                               ('duel-old-delivery', json.dumps(dict(submission, duel_id='duel-old-delivery')),
                                'prelim-ladder', json.dumps(result)))
            connection.execute('INSERT INTO games (id, duel, ordinal, bots, map) VALUES (1, 1, 1, ?, ?)',
                               (json.dumps(sides), json.dumps({'seed': 42})))
            connection.execute("INSERT INTO runs (run_id, game, attempt, submission) VALUES ('run-1.1', 1, 1, 'sending')")
            for ordinal, run_id in enumerate(result['runs'], 1):
                seats = sides if ordinal == 1 else sides[::-1]
                game = connection.execute('INSERT INTO games (duel, ordinal, bots, map) VALUES (2, ?, ?, ?)',
                                          (ordinal, json.dumps(seats), json.dumps({'seed': 42}))).lastrowid
                manifest = {'bots': [{'team_id': team} for team in seats],
                            'result': {'winner': seats.index('t001'), 'score': [8, 3] if ordinal == 1 else [3, 8]}}
                connection.execute('''INSERT INTO runs (run_id, game, attempt, submission, review, manifest)
                                      VALUES (?, ?, 1, 'accepted', 'effective', ?)''',
                                   (run_id, game, json.dumps(manifest)))
            connection.execute('INSERT INTO duels (id, duel_id, submission, kind) VALUES (3, ?, ?, ?)',
                               ('duel-old-review', json.dumps(dict(submission, duel_id='duel-old-review')), 'prelim-ladder'))
            connection.execute('INSERT INTO games (id, duel, ordinal, bots, map) VALUES (4, 3, 1, ?, ?)',
                               (json.dumps(sides), json.dumps({'seed': 42})))
            connection.execute('''INSERT INTO runs (run_id, game, attempt, submission, callback)
                                  VALUES ('run-4.1', 4, 1, 'accepted', ?)''',
                               (json.dumps({'state': 'platform_error', 'reason': 'wall_timeout'}),))

        contest = Contest(path)
        contest.runs.recover_submissions()
        worker = ContestWorker(contest, self.root, ArenaStub())
        self.assertTrue(worker.review_next())
        legacy_run = contest.duels.show('duel-old-review')['attempts'][0]
        self.assertEqual((legacy_run['review'], legacy_run['issue']), ('void', 'wall_timeout'))
        self.assertIsNone(contest.runs.next_review())
        self.assertEqual(contest.duels.show('duel-old-run')['attempts'][0]['submission'], 'unconfirmed')
        client = HttpClient(contest_handler(contest))
        query = '/contest/v1/duels/duel-old-delivery'
        self.assertEqual(client.json(query + '/status')['state'], 'finished')
        games = [{'ordinal': ordinal, 'run_id': run_id, 'winner': 't001', 'score': {'t001': 8, 'bot:L2': 3}}
                 for ordinal, run_id in enumerate(result['runs'], 1)]
        self.assertEqual(client.json(query + '/result'),
                         {'duel_id': 'duel-old-delivery', 'side_results': {'t001': 'win', 'bot:L2': 'loss'},
                          'result': dict(result, games=games, previous_duel_ids=[])})
        contest.runs.resend('run-1.1')
        self.assertEqual(contest.runs.claim(), {'run_id': 'run-1.1', 'duel_id': 'duel-old-run', 'rules': 'prelim',
                                               'bots': sides, 'config': {'map': {'seed': 42}}})
        contest.duels.accept(dict(submission, duel_id='duel-new', sides=['team001', 'bot002']))
        run = contest.runs.claim()
        self.assertEqual(run['duel_id'], 'duel-new')
        contest.runs.complete_submission(run['run_id'], 202)
        poll = contest.runs.claim_poll()
        self.assertEqual(poll.run_id, run['run_id'])
        contest.runs.complete_poll(poll, 200, {'run_id': run['run_id'], 'state': 'build_failed'})
        contest.runs.complete_review(run['run_id'], 'build_failed', manifest={
            'duel_started_at': '2026-11-20T10:00:00+08:00',
            'bots': [{'team_id': 'team001', 'state': 'succeeded'}, {'team_id': 'bot002', 'state': 'failed'}],
        })
        current = client.json('/contest/v1/duels/duel-new/result')['result']
        self.assertEqual(current['previous_duel_ids'], ['duel-old-delivery'])
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute('SELECT submission FROM duels WHERE id = 1').fetchone()[0],
                             json.dumps(submission))
            self.assertEqual(connection.execute('SELECT delivery FROM duels WHERE id = 2').fetchone()[0], 'sending')

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
        contest.runs.complete_poll(poll, 404, None, now=100)
        self.assertIsNone(contest.runs.claim_poll(now=100.5))
        self.assertEqual(contest.duels.show(duel_id)['attempts'][0]['submission'], 'unconfirmed')
        self.assertIsNone(contest.runs.claim())

        poll = contest.runs.claim_poll(now=101)
        contest.runs.complete_poll(poll, 200, {'run_id': run_id, 'state': 'queued'}, now=101)
        self.assertEqual(contest.duels.show(duel_id)['attempts'][0]['submission'], 'accepted')
        poll = contest.runs.claim_poll(now=102)
        contest.runs.complete_poll(poll, 404, None, now=102)
        missing = contest.duels.show(duel_id)['attempts'][0]
        self.assertIn('未找到已受理', missing['issue'])
        self.assertIsNone(missing['arena_result'])

        poll = contest.runs.claim_poll(now=103)
        contest.runs.complete_poll(poll, 503, None, now=103)
        self.assertIsNone(contest.runs.claim_poll(now=104))
        contest = Contest(path)
        poll = contest.runs.claim_poll(now=105)
        contest.runs.complete_poll(poll, 200, {'run_id': run_id, 'state': 'finished'}, now=105)
        first = contest.duels.show(duel_id)['attempts'][0]
        self.assertEqual((first['submission'], first['arena_result'], first['issue']),
                         ('accepted', {'state': 'finished'}, None))
        self.assertIsNone(contest.runs.claim())
        self.assertEqual(Contest(path).runs.next_review()[0], request)
