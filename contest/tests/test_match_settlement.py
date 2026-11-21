import hashlib
import json

from .helpers import (TEAMS, Bot, HttpClient, MatchFixture, ArenaStub,
                     admin_headers, arena_archive, case, contest_handler, run_module, run_query_payload, sealed_maps)

from service.application import Contest, ContestWorker
from service.duels import EffectiveGame, load_pools, settle
from service.executions import review_archive
from service.server import Admin
from engine.core import ENGINE_VERSION, TIEBREAK_METRICS
from engine.mapgen import generate
from engine.protocol import dumps
from engine.replay import VerificationError, game_result, verify_replay


STARTED = '2026-11-01T14:05:00+08:00'
DUELS_PATH = '/contest/v1/duels'


class MatchSettlementTests(MatchFixture):
    def poll_and_review(self, arena, worker, run_id, state, *, preliminary=False):
        arena.set_state(run_id, state)
        if preliminary:
            worker.poll_preliminary_round()
        else:
            self.assertTrue(worker.poll_next())
        self.assertTrue(worker.review_next())

    @case('L0-002', '单局全同和启动双崩溃在通过复核后进入待裁定')
    def test_all_equal_at_limit_or_on_double_startup_failure(self):
        contest = Contest(self.root / 'ties.sqlite', {stage: sealed_maps(stage) for stage in ('prelim', 'final')})
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        for number, stage in enumerate(('prelim', 'final'), 1):
            with self.subTest(stage=stage):
                duel_id = f'duel-{number}'
                contest.duels.accept({'duel_id': duel_id, 'rules': stage, 'sides': TEAMS})
                self.assertTrue(worker.submit_next())
                request = arena.submitted[-1]
                bots = [Bot('U'), Bot('D')] if stage == 'prelim' else None
                _, manifest, replay = self.judge_archive(request, bots)
                self.assertEqual(manifest['result']['engine_version'], ENGINE_VERSION)
                self.assertIsNone(manifest['result']['winner'])
                self.assertEqual(manifest['result']['reason'], 'tie' if stage == 'prelim' else 'crash')
                self.assertEqual(replay['result']['turns'], 600 if stage == 'prelim' else 0)
                if stage == 'final':
                    self.assertEqual(replay['observations'][0]['exits'], [True, True])
                    self.assertEqual(game_result(verify_replay(replay))['time_us'], [0, 0])
                self.poll_and_review(arena, worker, request['run_id'], 'finished', preliminary=stage == 'prelim')
                stored = contest.duels.show(duel_id)
                self.assertEqual(stored['attempts'][0]['review'], 'awaiting_ruling')
                self.assertIn('四项指标全同', stored['attempts'][0]['issue'])
                self.assertIsNone(stored['result'])
                self.assertFalse(worker.submit_next())
                self.assertIsNone(contest.duels.result(duel_id)['result'])

    @case('L0-003', '触发技术判负的回合保留整数微秒计时')
    def test_technical_forfeit_turn_keeps_exact_microseconds(self):
        request = self.request()
        directory, manifest, replay = self.judge_archive(request, [
            Bot(None, status='timeout', us=201234, init='invalid'), Bot('D', us=1001),
        ])
        self.assertEqual(replay['frames'][-1]['turn'], 8)
        self.assertEqual(replay['frames'][-1]['ms'][1], 1.0)
        self.assertEqual(replay['observations'][-2], {'turn': 9, 'status': ['timeout', 'ok'],
                                                      'us': [200000, 1001], 'exits': [False, False]})
        self.assertEqual(manifest['result']['time_us'], [1800000, 9009])
        self.assertEqual(manifest['result']['winner'], 1)
        self.assertNotIn('private-response', dumps(replay['observations']))
        self.assertEqual(review_archive(self.root, request, 'finished', ladder=False), manifest)
        self.assertEqual(game_result(verify_replay(replay))['time_us'], [1800000, 9009])

        manifest['result']['time_us'][1] = 9000
        (directory / 'match.json').write_text(dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(VerificationError, 'time_us'):
            review_archive(self.root, request, 'finished', ladder=False)

    @case('L0-004', '同回合双判负包含最后已发请求的耗时')
    def test_double_forfeit_uses_the_issued_requests_from_the_last_turn(self):
        _, manifest, replay = self.judge_archive(self.request(), [
            Bot(None, status='invalid', init='invalid', us=lambda turn: 1001 if turn < 9 else 1970),
            Bot(None, status='invalid', init='invalid', us=lambda turn: 1002 if turn < 9 else 20),
        ])
        self.assertEqual(replay['frames'][-1]['turn'], 8)
        self.assertEqual(manifest['result']['time_us'], [9978, 8036])
        self.assertEqual(verify_replay(replay).winner, 1)

    @case('L2-002', '天梯也要求当前引擎和完整的公开裁判观测')
    def test_ladder_archive_must_have_public_evidence_from_the_current_engine(self):
        request = self.request()
        output, manifest, replay = self.judge_archive(request, started_at=STARTED)
        self.assertEqual(review_archive(self.root, request, 'finished', ladder=True), manifest)
        replay.pop('observations')
        (output / 'replay.json').write_text(dumps(replay), encoding='utf-8')
        with self.assertRaisesRegex(VerificationError, '缺少完整的裁判观测'):
            review_archive(self.root, request, 'finished', ladder=True)
        manifest['result']['engine_version'] = 'exposure-field/2'
        (output / 'match.json').write_text(dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(VerificationError, '无可用的同版引擎'):
            review_archive(self.root, request, 'finished', ladder=True)

    @case('L0-005', '正式对决三比三按四项指标顺序决胜')
    def test_formal_tiebreak_order_and_all_equal(self):
        for deciding in (*TIEBREAK_METRICS, None):
            with self.subTest(deciding=deciding):
                metrics = {key: [20, 20] for key in TIEBREAK_METRICS}
                if deciding is not None:
                    favorable = [10, 20] if deciding == 'time_us' else [20, 10]
                    metrics[deciding] = favorable
                    for key in TIEBREAK_METRICS[TIEBREAK_METRICS.index(deciding) + 1:]:
                        metrics[key] = [20, 10] if key == 'time_us' else [10, 20]
                runs = [EffectiveGame(i, f'run-{i}.1', {'bots': [{'team_id': team} for team in TEAMS],
                                                      'result': dict(metrics, winner=i % 2)}) for i in range(1, 7)]
                settled = settle(TEAMS, False, runs)
                if deciding is None:
                    self.assertIsNone(settled)
                else:
                    self.assertEqual(settled['winner'], 'team001')
                    self.assertEqual(settled['tiebreak'], deciding)
                    self.assertEqual(settled['games_won'], dict.fromkeys(TEAMS, 3))
                    self.assertEqual(settled['games_lost'], dict.fromkeys(TEAMS, 3))

    @case('L0-006', 'Portal 按局序查询六局复核赛果且重复请求幂等')
    def test_portal_queries_six_verified_games_with_seat_swaps(self):
        entries = []
        for seed in (42, 43, 44):
            board = generate(seed, 'final')
            file = self.root / f'{seed}.json'
            data = (json.dumps(board, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
            file.write_bytes(data)
            entries.append({'file': file.name, 'sha256': hashlib.sha256(data).hexdigest()})
        pool_file = self.root / 'pools.json'
        pool_file.write_text(dumps({'final': entries}), encoding='utf-8')
        db = self.root / 'contest.sqlite'
        contest = Contest(db, load_pools(pool_file))
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)

        client = HttpClient(contest_handler(contest))

        duel = {'duel_id': 'duel-1', 'rules': 'final', 'sides': TEAMS}
        self.assertEqual(client.post(DUELS_PATH, duel), (202, b''))
        self.assertEqual(client.post(DUELS_PATH, duel), (202, b''))
        self.assertEqual(client.post(DUELS_PATH, dict(duel, sides=TEAMS[::-1]))[0], 409)
        game_results = []
        duel_path = DUELS_PATH + '/duel-1'
        for ordinal in range(6):
            self.assertTrue(worker.submit_next())
            request = arena.submitted[-1]
            run_id = request['run_id']
            self.assertEqual(request['bots'], TEAMS if ordinal % 2 == 0 else TEAMS[::-1])
            self.assertEqual(request['config']['map']['sha256'], entries[ordinal // 2]['sha256'])
            us = [1201 if team == 'team001' else 901 for team in request['bots']]
            _, manifest, _ = self.judge_archive(
                request, [Bot('U', us=us[0]), Bot('D', us=us[1], exit_after_reply=True)])
            game_results.append({'ordinal': ordinal + 1, 'run_id': run_id, 'winner': request['bots'][0],
                                 'score': dict(zip(request['bots'], manifest['result']['score']))})
            self.poll_and_review(arena, worker, run_id, 'finished')
            if ordinal < 5:
                self.assertEqual(client.json(duel_path + '/status')['state'], 'in_progress')
                self.assertIsNone(client.json(duel_path + '/result')['result'])

        self.assertFalse(worker.submit_next())
        self.assertEqual(len(arena.submitted), 6)
        self.assertEqual(client.json(duel_path + '/status'), {'duel_id': 'duel-1', 'state': 'finished'})
        response = client.json(duel_path + '/result')
        self.assertEqual(response['side_results'], {'team001': 'loss', 'team002': 'win'})
        result = response['result']
        self.assertEqual(result['winner'], 'team002')
        self.assertEqual(result['tiebreak'], 'time_us')
        self.assertEqual(result['games_won'], dict.fromkeys(TEAMS, 3))
        self.assertEqual(result['games_lost'], dict.fromkeys(TEAMS, 3))
        self.assertEqual(result['games'], game_results)
        self.assertEqual(result['runs'], [game['run_id'] for game in game_results])
        self.assertNotIn('previous_duel_ids', result)
        self.assertEqual(HttpClient(contest_handler(Contest(db))).json(duel_path + '/result'), response)

    @case('L0-016', '天梯两局按换边归队结算胜负和平局')
    def test_ladder_settles_two_games_as_win_or_draw(self):
        contest = Contest(self.root / 'ladder.sqlite')
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        client = HttpClient(contest_handler(contest))
        for number, winners in enumerate((TEAMS, [TEAMS[0], TEAMS[0]]), 1):
            with self.subTest(winners=winners):
                duel_id = f'duel-{number}'
                contest.duels.accept({'duel_id': duel_id, 'rules': 'prelim', 'sides': TEAMS, 'map_seed': 42})
                run_ids = []
                for ordinal, winner in enumerate(winners):
                    self.assertTrue(worker.submit_next())
                    request = arena.submitted[-1]
                    self.assertEqual(request['bots'], TEAMS if ordinal == 0 else TEAMS[::-1])
                    bots = [Bot(command, exit_after_reply=team != winner)
                            for command, team in zip(('U', 'D'), request['bots'])]
                    self.judge_archive(request, bots, started_at=STARTED)
                    self.poll_and_review(arena, worker, request['run_id'], 'finished')
                    run_ids.append(request['run_id'])
                    if ordinal == 0:
                        self.assertIsNone(contest.duels.show(duel_id)['result'])
                response = client.json(f'{DUELS_PATH}/{duel_id}/result')
                result = response['result']
                self.assertEqual(result['runs'], run_ids)
                self.assertEqual(result['started_at'], STARTED)
                won = {team: winners.count(team) for team in TEAMS}
                self.assertEqual(result['games_won'], won)
                self.assertEqual(result['games_lost'], {team: 2 - won[team] for team in TEAMS})
                self.assertEqual((result['outcome'], result['winner']),
                                 ('draw', None) if won[TEAMS[0]] == 1 else ('win', TEAMS[0]))
                self.assertEqual(response['side_results'],
                                 dict.fromkeys(TEAMS, 'draw') if number == 1 else {'team001': 'win', 'team002': 'loss'})
                self.assertEqual(result['previous_duel_ids'], [] if number == 1 else ['duel-1'])
                self.assertFalse(worker.submit_next())

    @case('L0-017', '正式与天梯构建单败或双败均按整场局数结算')
    def test_build_failures_settle_the_whole_duel(self):
        contest = Contest(self.root / 'builds.sqlite', {stage: sealed_maps(stage) for stage in ('prelim', 'final')})
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        client = HttpClient(contest_handler(contest))
        for stage, ladder in (('prelim', True), ('prelim', False), ('final', False)):
            for failed in ((0,), (1,), (0, 1)):
                with self.subTest(stage=stage, ladder=ladder, failed=failed):
                    duel_id = f'duel-build-{stage}-{int(ladder)}-' + ''.join(map(str, failed))
                    body = {'duel_id': duel_id, 'rules': stage, 'sides': TEAMS}
                    if ladder:
                        body['map_seed'] = 42
                    contest.duels.accept(body)
                    self.assertTrue(worker.submit_next())
                    request = arena.submitted[-1]
                    output = self.root / 'runs' / request['run_id']
                    output.mkdir(parents=True)
                    bots = [dict(bot, state='failed' if seat in failed else 'succeeded')
                            for seat, bot in enumerate(self.metadata(request))]
                    arena_archive(output, bots, state='build_failed',
                                       duel_started_at=STARTED if ladder else None)
                    self.poll_and_review(arena, worker, request['run_id'], 'build_failed',
                                         preliminary=stage == 'prelim' and not ladder)
                    path = f'{DUELS_PATH}/{duel_id}'
                    response = client.json(path + '/result')
                    result = response['result']
                    scheduled = 2 if ladder else 6
                    winner = None if len(failed) == 2 else TEAMS[1 - failed[0]]
                    self.assertEqual((result['outcome'], result['winner']),
                                     ('double_loss' if winner is None else 'win', winner))
                    self.assertEqual(result['games_won'], {team: scheduled if team == winner else 0 for team in TEAMS})
                    self.assertEqual(result['games_lost'],
                                     {team: scheduled if seat in failed else 0 for seat, team in enumerate(TEAMS)})
                    self.assertEqual((result['build_run_id'], result['runs']), (request['run_id'], []))
                    self.assertEqual(result['games'], [])
                    self.assertEqual(response['side_results'], {team: 'win' if team == winner else 'loss' for team in TEAMS})
                    self.assertEqual(client.json(path + '/status')['state'], 'finished')
                    self.assertEqual(contest.duels.show(duel_id)['attempts'][0]['review'], 'build_failed')
                    self.assertFalse(worker.submit_next())

    @case('L0-020', '决赛三比三累计指标全同后可录入整场裁定，Portal 取得胜方')
    def test_formal_equal_totals_can_be_ruled_after_six_effective_games(self):
        db = self.root / 'formal-tie.sqlite'
        contest = Contest(db, {'final': sealed_maps('final')})
        contest.duels.accept({'duel_id': 'duel-1', 'rules': 'final', 'sides': TEAMS})
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        for _ in range(6):
            self.assertTrue(worker.submit_next())
            request = arena.submitted[-1]
            self.judge_archive(request, [Bot('U'), Bot('D', exit_after_reply=True)])
            self.poll_and_review(arena, worker, request['run_id'], 'finished')
        stored = contest.duels.show('duel-1')
        self.assertEqual([run['review'] for run in stored['attempts']], ['effective'] * 6)
        self.assertIsNone(stored['result'])
        self.assertIn('整场四项累计指标全同', stored['issue'])
        self.assertFalse(worker.submit_next())
        client = HttpClient(contest_handler(contest))
        self.assertEqual(client.json(DUELS_PATH + '/duel-1/status')['state'], 'in_progress')
        self.assertEqual(client.json(DUELS_PATH + '/duel-1/result')['side_results'], dict.fromkeys(TEAMS, 'pending'))
        pending = json.loads(run_module('service', '--db', db, 'rulings').stdout)['pending']
        self.assertEqual([(item['scope'], item['duel_id']) for item in pending], [('duel', 'duel-1')])
        reason = '主办方按已公示的决胜程序核定'
        ruling = json.loads(run_module('service', '--db', db, 'rule', 'duel', 'duel-1', TEAMS[1], reason).stdout)
        self.assertTrue(ruling['operator'])
        self.assertEqual(contest.duels.show('duel-1')['ruling'], ruling)
        self.assertEqual(contest.rulings.pending(), {'pending': []})
        self.assertEqual(client.json(DUELS_PATH + '/duel-1/status')['state'], 'finished')
        result = client.json(DUELS_PATH + '/duel-1/result')['result']
        self.assertEqual((result['winner'], result['reason'], result['games_won'], result['games_lost']),
                         (TEAMS[1], reason, dict.fromkeys(TEAMS, 3), dict.fromkeys(TEAMS, 3)))
        self.assertEqual(len(result['games']), 6)
        self.assertNotIn('operator', result)

    @case('L0-028', '决赛资格单败和双败可录入，保留历史生效单局并关闭当前尝试')
    def test_final_eligibility_forfeit_settles_and_preserves_effective_games(self):
        db = self.root / 'forfeits.sqlite'
        contest = Contest(db, {'final': sealed_maps('final')})
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        admin = Admin(contest, 'organizer', 'test-password', arena)
        handler = contest_handler(contest, admin=admin)
        portal = HttpClient(handler)
        organizer = HttpClient(handler, prefix='/admin', headers=admin_headers('organizer', 'test-password'))
        first, second = 'duel-final-a', 'duel-final-b'
        for duel_id in (first, second):
            self.assertEqual(portal.post(DUELS_PATH, {'duel_id': duel_id, 'rules': 'final', 'sides': TEAMS}), (202, b''))
        path = 'final/forfeits'
        self.assertEqual(organizer.json(path)['duels'], [{'duel_id': duel_id, 'sides': TEAMS}
                                                        for duel_id in (first, second)])
        self.assertTrue(worker.submit_next())
        request = arena.submitted[-1]
        output, _, _ = self.judge_archive(request, [Bot('U'), Bot('D', exit_after_reply=True)])
        evidence = (output / 'replay.json').read_bytes()
        self.poll_and_review(arena, worker, request['run_id'], 'finished')
        self.assertTrue(worker.submit_next())
        in_flight = contest.runs.claim_poll()
        self.assertEqual(in_flight.run_id, arena.submitted[-1]['run_id'])

        body = {'duel_id': first, 'defeated': [TEAMS[0]], 'reason': '选手确认退赛'}
        self.assertEqual(HttpClient(handler).post('/admin/' + path, body)[0], 401)
        self.assertEqual(organizer.post(path, dict(body, defeated=['team-outside']))[0], 400)
        ruling = organizer.json(path, body, method='POST')
        self.assertEqual((ruling['scope'], ruling['operator'], ruling['winner']), ('forfeit', 'organizer', TEAMS[1]))
        self.assertEqual(organizer.json(path, body, method='POST'), ruling)
        self.assertEqual(organizer.post(path, dict(body, defeated=[TEAMS[1]]))[0], 409)
        contest.runs.complete_polls((in_flight,), 200,
                                    run_query_payload({'run_id': in_flight.run_id, 'state': 'finished'}))
        self.assertFalse(worker.review_next())
        self.assertEqual(contest.duels.show(first)['attempts'][1]['arena_result'], None)
        self.assertEqual((output / 'replay.json').read_bytes(), evidence)
        self.assertEqual(portal.json(f'{DUELS_PATH}/{first}/status')['state'], 'finished')
        settled = portal.json(f'{DUELS_PATH}/{first}/result')
        self.assertEqual(settled['side_results'], {TEAMS[0]: 'loss', TEAMS[1]: 'win'})
        result = settled['result']
        self.assertEqual((result['winner'], result['forfeited'], result['reason']), (TEAMS[1], body['defeated'], body['reason']))
        self.assertEqual(result['games_won'], {TEAMS[0]: 0, TEAMS[1]: 6})
        self.assertEqual(result['games_lost'], {TEAMS[0]: 6, TEAMS[1]: 0})
        self.assertEqual(result['runs'], [request['run_id']])
        game, = result['games']
        self.assertEqual((game['ordinal'], game['run_id'], game['winner']), (1, request['run_id'], TEAMS[0]))

        self.assertEqual(organizer.json(path)['duels'], [{'duel_id': second, 'sides': TEAMS}])
        next_run = contest.runs.claim()
        self.assertEqual(next_run['duel_id'], second)
        reason = '双方均丧失参赛资格'
        decision = organizer.json(path, {'duel_id': second, 'defeated': TEAMS[::-1], 'reason': reason}, method='POST')
        self.assertEqual(decision['forfeited'], sorted(TEAMS))
        self.assertIsNone(decision['winner'])
        self.assertEqual(organizer.json(path, {'duel_id': second, 'defeated': TEAMS, 'reason': reason}, method='POST'), decision)
        contest.runs.complete_submission(next_run['run_id'], 202)
        self.assertIsNone(contest.runs.claim_poll())
        self.assertFalse(worker.submit_next())
        self.assertEqual(organizer.json(path), {'duels': []})
        double_loss = portal.json(f'{DUELS_PATH}/{second}/result')
        self.assertEqual(double_loss['side_results'], dict.fromkeys(TEAMS, 'loss'))
        self.assertEqual(double_loss['result']['outcome'], 'double_loss')
        self.assertEqual(double_loss['result']['games'], [])
        self.assertEqual(double_loss['result']['games_lost'], dict.fromkeys(TEAMS, 6))

    @case('L0-007', '故障重跑固定原计划且旧轮询响应不能生效')
    def test_retry_pins_the_plan_and_ignores_a_stale_poll(self):
        contest = Contest(self.root / 'contest.sqlite')
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        sides = ['team001', 'bot002']
        contest.duels.accept({'duel_id': 'duel-1', 'rules': 'prelim', 'sides': sides, 'map_seed': 42})
        self.assertTrue(worker.submit_next())
        original = arena.submitted[-1]
        stale_poll = contest.runs.claim_poll()
        self.assertEqual(stale_poll.run_id, original['run_id'])
        contest.runs.invalidate(original['run_id'], '运行资源不符合要求')
        replacement = contest.runs.retry(original['run_id'])
        self.assertEqual(replacement, 'run-1.2')
        contest.runs.complete_polls((stale_poll,), 200, run_query_payload({
            'run_id': original['run_id'], 'state': 'finished',
        }))
        self.assertTrue(worker.submit_next())
        self.assertEqual(arena.submitted[-1], dict(original, run_id=replacement))

        self.judge_archive(arena.submitted[-1], [Bot('U'), Bot('D', exit_after_reply=True)], started_at=STARTED)
        self.poll_and_review(arena, worker, replacement, 'finished')
        self.assertIsNone(contest.duels.result('duel-1')['result'])

        self.assertTrue(worker.submit_next())
        second = arena.submitted[-1]
        directory = self.root / 'runs' / second['run_id']
        directory.mkdir(parents=True)
        bots = [dict(bot, state='failed') for bot in self.metadata(second)]
        arena_archive(directory, bots, state='build_failed', duel_started_at=STARTED)
        self.poll_and_review(arena, worker, second['run_id'], 'build_failed')
        self.assertFalse(worker.submit_next())
        client = HttpClient(contest_handler(contest))
        result = client.json(DUELS_PATH + '/duel-1/result')['result']
        self.assertEqual(result['outcome'], 'double_loss')
        self.assertEqual(result['games_lost'], dict.fromkeys(sides, 2))
        self.assertEqual(result['build_run_id'], second['run_id'])
        self.assertEqual(result['runs'], [replacement])
        self.assertEqual([(game['ordinal'], game['run_id']) for game in result['games']], [(1, replacement)])
        self.assertEqual(result['started_at'], STARTED)
        old = contest.duels.show('duel-1')['attempts'][0]
        self.assertEqual(old['review'], 'void')
        self.assertIsNone(old['arena_result'])

    @case('L0-008', '天梯乱序读取时返回同赛段双方最近确认的前序对决')
    def test_ladder_queries_link_each_sides_previous_confirmed_duel(self):
        contest = Contest(self.root / 'contest.sqlite')
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        contest.duels.accept({'duel_id': 'duel-deferred', 'rules': 'prelim', 'sides': TEAMS, 'map_seed': 42})
        self.assertTrue(worker.submit_next())
        deferred_run = arena.submitted[-1]['run_id']
        contest.runs.invalidate(deferred_run, '平台故障待重跑')

        def finish_after_build_failure(duel_id):
            self.assertTrue(worker.submit_next())
            request = arena.submitted[-1]
            self.assertEqual(request['duel_id'], duel_id)
            output = self.root / 'runs' / request['run_id']
            output.mkdir(parents=True)
            bots = [dict(bot, state=state) for bot, state in zip(self.metadata(request), ('succeeded', 'failed'))]
            arena_archive(output, bots, state='build_failed', duel_started_at=STARTED)
            self.poll_and_review(arena, worker, request['run_id'], 'build_failed')

        duels = (
            ('duel-earliest', 'prelim', ['team001', 'team003']),
            ('duel-left', 'prelim', ['team001', 'bot002']),
            ('duel-right', 'prelim', ['team002', 'team004']),
            ('duel-other-stage', 'final', TEAMS),
            ('duel-both', 'prelim', TEAMS),
            ('duel-again', 'prelim', TEAMS),
        )
        for duel_id, stage, sides in duels:
            contest.duels.accept({'duel_id': duel_id, 'rules': stage, 'sides': sides, 'map_seed': 42})
            finish_after_build_failure(duel_id)
        contest.runs.retry(deferred_run)
        finish_after_build_failure('duel-deferred')
        self.assertFalse(worker.submit_next())
        client = HttpClient(contest_handler(contest))
        expected = (
            ('duel-deferred', ['duel-again']),
            ('duel-again', ['duel-both']),
            ('duel-both', ['duel-left', 'duel-right']),
            ('duel-other-stage', []),
            ('duel-right', []),
            ('duel-left', ['duel-earliest']),
            ('duel-earliest', []),
        )
        ended_at = []
        for duel_id, predecessors in expected:
            with self.subTest(duel_id=duel_id):
                result = client.json(f'{DUELS_PATH}/{duel_id}/result')['result']
                self.assertEqual(result['previous_duel_ids'], predecessors)
                ended_at.append(result['ended_at'])
        self.assertEqual(ended_at, sorted(set(ended_at), reverse=True))
