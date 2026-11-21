from .helpers import (TEAMS, ArenaStub, Bot, HttpClient, MatchFixture, arena_archive, case,
                      contest_handler, plan_duel, run_query_payload, sealed_maps)

from service.arena import review_archive
from service.duels import EffectiveGame, settle
from service.server import Contest, ContestWorker
from engine.core import TIEBREAK_METRICS
from engine.protocol import dumps
from engine.replay import game_result
from engine.verification import VerificationError, verify_replay


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

    @case('L0-006', '统一接口受理决赛并固定六局计划，重启后照原计划结算')
    def test_final_submitted_by_portal_finishes_using_its_stored_plan(self):
        maps = sealed_maps('final')
        db = self.root / 'contest.sqlite'
        client = HttpClient(contest_handler(Contest(db, {'final': maps}, phase='final')))
        duel_id = client.json(DUELS_PATH, {'sides': TEAMS}, method='POST', status=202)['duel_id']
        contest = Contest(db, phase='prelim-ladder')
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        client = HttpClient(contest_handler(contest))
        game_results = []
        duel_path = f'{DUELS_PATH}/{duel_id}'
        self.assertEqual(client.json(DUELS_PATH, {'sides': TEAMS}, method='POST', status=409)['code'], 'PHASE_BUSY')
        for ordinal in range(6):
            self.assertTrue(worker.submit_next())
            request = arena.submitted[-1]
            run_id = request['run_id']
            if ordinal == 0:
                first_bots = request['bots']
                self.assertCountEqual(first_bots, TEAMS)
            self.assertEqual(request['bots'], first_bots if ordinal % 2 == 0 else first_bots[::-1])
            self.assertEqual((request['rules'], request['config']['map']), ('final', maps[ordinal // 2]))
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
        self.assertEqual(client.json(duel_path + '/status'), {'duel_id': duel_id, 'state': 'finished'})
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
        self.assertIn('duel_id', client.json(DUELS_PATH, {'sides': TEAMS}, method='POST', status=202))

    @case('L0-016', '决赛规则的天梯对决同图两局换边，结算胜负和平局')
    def test_final_ladder_settles_two_games_as_win_or_draw(self):
        contest = Contest(self.root / 'ladder.sqlite', phase='final-ladder')
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        client = HttpClient(contest_handler(contest))
        previous = []
        for number, winners in enumerate((TEAMS, [TEAMS[0], TEAMS[0]]), 1):
            with self.subTest(winners=winners):
                duel_id = client.json(DUELS_PATH, {'sides': TEAMS}, method='POST', status=202)['duel_id']
                run_ids = []
                for ordinal, winner in enumerate(winners):
                    self.assertTrue(worker.submit_next())
                    request = arena.submitted[-1]
                    if ordinal == 0:
                        first_request = request
                        self.assertCountEqual(first_request['bots'], TEAMS)
                    else:
                        self.assertEqual(request['bots'], first_request['bots'][::-1])
                        self.assertEqual(request['config'], first_request['config'])
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
                self.assertEqual(result['previous_duel_ids'], previous)
                previous = [duel_id]
                self.assertFalse(worker.submit_next())

    @case('L0-017', '正式与天梯构建单败或双败均按整场局数结算')
    def test_build_failures_settle_the_whole_duel(self):
        contest = Contest(self.root / 'builds.sqlite', {'prelim': sealed_maps('prelim')})
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        client = HttpClient(contest_handler(contest))
        for stage, ladder in (('prelim', True), ('prelim', False), ('final', True)):
            for failed in ((0,), (1,), (0, 1)):
                with self.subTest(stage=stage, ladder=ladder, failed=failed):
                    duel_id = f'duel-build-{stage}-{int(ladder)}-' + ''.join(map(str, failed))
                    plan_duel(contest, duel_id, stage + '-ladder' if ladder else stage)
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

    @case('L0-007', '故障重跑固定原计划，旧轮询响应和旧执行编号均不能生效')
    def test_retry_pins_the_plan_and_ignores_a_stale_poll(self):
        contest = Contest(self.root / 'contest.sqlite')
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        sides = ['team001', 'bot002']
        duel_id = contest.duels.accept({'sides': sides})['duel_id']
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

        output, manifest, _ = self.judge_archive(
            arena.submitted[-1], [Bot('U'), Bot('D', exit_after_reply=True)], started_at=STARTED)
        evidence = output / 'match.json'
        wrong_result = dict(manifest['result'], run_id=original['run_id'])
        evidence.write_text(dumps(dict(manifest, result=wrong_result)), encoding='utf-8')
        self.poll_and_review(arena, worker, replacement, 'finished')
        held = contest.duels.show(duel_id)['attempts'][1]
        self.assertEqual((held['run_id'], held['review']), (replacement, 'held'))
        self.assertIn('result.run_id', held['issue'])
        self.assertFalse(worker.submit_next())
        self.assertIsNone(contest.duels.result(duel_id)['result'])

        evidence.write_text(dumps(manifest), encoding='utf-8')
        contest.runs.recheck(replacement)
        self.assertTrue(worker.review_next())
        self.assertEqual(contest.duels.show(duel_id)['attempts'][1]['review'], 'effective')

        self.assertTrue(worker.submit_next())
        second = arena.submitted[-1]
        directory = self.root / 'runs' / second['run_id']
        directory.mkdir(parents=True)
        bots = [dict(bot, state='failed') for bot in self.metadata(second)]
        arena_archive(directory, bots, state='build_failed', duel_started_at=STARTED)
        self.poll_and_review(arena, worker, second['run_id'], 'build_failed')
        self.assertFalse(worker.submit_next())
        client = HttpClient(contest_handler(contest))
        result = client.json(f'{DUELS_PATH}/{duel_id}/result')['result']
        self.assertEqual(result['outcome'], 'double_loss')
        self.assertEqual(result['games_lost'], dict.fromkeys(sides, 2))
        self.assertEqual(result['build_run_id'], second['run_id'])
        self.assertEqual(result['runs'], [replacement])
        self.assertEqual([(game['ordinal'], game['run_id']) for game in result['games']], [(1, replacement)])
        self.assertEqual(result['started_at'], STARTED)
        old = contest.duels.show(duel_id)['attempts'][0]
        self.assertEqual(old['review'], 'void')
        self.assertIsNone(old['arena_result'])

    @case('L0-008', '天梯乱序读取时返回同赛段双方最近确认的前序对决')
    def test_ladder_queries_link_each_sides_previous_confirmed_duel(self):
        contest = Contest(self.root / 'contest.sqlite')
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        plan_duel(contest, 'duel-deferred', 'prelim-ladder')
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
            ('duel-both', 'prelim', TEAMS),
            ('duel-again', 'prelim', TEAMS),
        )
        for duel_id, stage, sides in duels:
            plan_duel(contest, duel_id, stage + '-ladder', sides)
            finish_after_build_failure(duel_id)
        contest.runs.retry(deferred_run)
        finish_after_build_failure('duel-deferred')
        plan_duel(contest, 'duel-other-stage', 'final-ladder')
        finish_after_build_failure('duel-other-stage')
        self.assertFalse(worker.submit_next())
        client = HttpClient(contest_handler(contest))
        expected = (
            ('duel-other-stage', []),
            ('duel-deferred', ['duel-again']),
            ('duel-again', ['duel-both']),
            ('duel-both', ['duel-left', 'duel-right']),
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
