import json

from .helpers import (TEAMS, ArenaStub, Bot, HttpClient, MatchFixture, arena_archive, case,
                      contest_handler, plan_duel, run_query_payload, sealed_maps)

from service.arena import review_archive
from service.duels import EffectiveGame, settle
from service.server import Contest, ContestWorker
from engine.protocol import dumps
from engine.replay import VerificationError, game_result, verify_replay
from engine.rules import TIEBREAK_METRICS


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
        self.assertIsNone(verify_replay(replay).winner)
        self.assertEqual(manifest['result']['reason'], 'double_loss')

    @case('L0-005', '正式对决三比三按三项指标顺序决胜')
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
                                                      'result': dict(metrics, winner=i % 2, turns=600)}) for i in range(1, 7)]
                settled = settle(TEAMS, False, runs)
                if deciding is None:
                    self.assertIsNone(settled)
                else:
                    self.assertEqual(settled['winner'], 'team001')
                    self.assertEqual(settled['tiebreak'], deciding)
                    self.assertEqual(settled['games_won'], dict.fromkeys(TEAMS, 3))
                    self.assertEqual(settled['games_lost'], dict.fromkeys(TEAMS, 3))

    @case('L0-043', '天梯开局同时技术判负的两局全部生效，双方各记两负')
    def test_double_losses_are_effective(self):
        contest = Contest(self.root / 'contest.sqlite')
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        duel_id = contest.duels.accept({'sides': TEAMS})['duel_id']
        for _ in range(2):
            self.assertTrue(worker.submit_next())
            request = arena.submitted[-1]
            _, manifest, _ = self.judge_archive(request, started_at=STARTED)
            self.assertEqual((manifest['result']['winner'], manifest['result']['reason'],
                              manifest['result']['turns']), (None, 'double_loss', 0))
            self.poll_and_review(arena, worker, request['run_id'], 'finished')
        self.assertFalse(contest.rulings.pending()['pending'])
        response = contest.duels.result(duel_id)
        result = response['result']
        self.assertEqual(response['side_results'], dict.fromkeys(TEAMS, 'loss'))
        self.assertEqual((result['outcome'], result['winner']), ('double_loss', None))
        self.assertEqual(result['games_won'], dict.fromkeys(TEAMS, 0))
        self.assertEqual(result['games_lost'], dict.fromkeys(TEAMS, 2))
        self.assertTrue(all(g['winner'] is None and g['reason'] == 'double_loss' for g in result['games']))

    @case('L0-044', '初赛两胜四负及天梯一比一只汇总打满回合的单局')
    def test_only_completed_games_contribute_to_tiebreaks(self):
        def played(n, teams, winner, turns, score):
            reason = 'double_loss' if winner is None else 'limit' if turns == 600 else 'crash'
            return EffectiveGame(n, f'run-{n}.1', {
                'bots': [{'team_id': team} for team in teams],
                'result': {'winner': winner, 'reason': reason, 'turns': turns, 'score': score,
                           'lead_turns': [0, 0], 'time_us': [5, 5]},
            })

        early = played(1, TEAMS, 0, 599, [999, 0])
        full = played(2, TEAMS[::-1], 0, 600, [12, 10])
        ladder = settle(TEAMS, True, [early, full])
        self.assertEqual((ladder['winner'], ladder['tiebreak']), (TEAMS[1], 'score'))
        no_full_games = [early, played(2, TEAMS[::-1], 0, 599, [42, 24])]
        self.assertIsNone(settle(TEAMS, True, no_full_games))
        official = [early, full, played(3, TEAMS, 0, 1, [9, 9]),
                    played(4, TEAMS[::-1], 0, 15, [10, 8]),
                    played(5, TEAMS, None, 50, [100, 9]), played(6, TEAMS[::-1], None, 0, [9, 9])]
        result = settle(TEAMS, False, official)
        self.assertEqual((result['winner'], result['games_won'], result['games_lost']),
                         (TEAMS[1], dict.fromkeys(TEAMS, 2), dict.fromkeys(TEAMS, 4)))

    @case('L0-046', '复核兼容 Arena 清单省略回合数')
    def test_review_restores_turns_from_replay(self):
        request = self.request()
        output, manifest, _ = self.judge_archive(request, [Bot('U', us=1000), Bot('D', us=2000)])
        result = manifest['result']
        result.pop('turns')
        (output / 'match.json').write_text(dumps(manifest), encoding='utf-8')
        reviewed = review_archive(self.root, request, 'finished', ladder=False)
        self.assertEqual((reviewed['result']['turns'], reviewed['result']['reason']), (600, 'time'))

    @case('L0-048', '同队初赛模拟全同后按出生点裁定，不登记平局')
    def test_self_play_rulings_by_seat(self):
        contest = Contest(self.root / 'contest.sqlite')
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        duel = contest.duels.accept_manual({'sides': [TEAMS[0], TEAMS[0]], 'rules': 'prelim'})['duel_id']
        for seat in ('0', '1'):
            self.assertTrue(worker.submit_next())
            request = arena.submitted[-1]
            self.judge_archive(request, [Bot('U', us=1000), Bot('D', us=1000)], started_at=STARTED)
            self.poll_and_review(arena, worker, request['run_id'], 'finished')
            pending, = contest.rulings.pending()['pending']
            self.assertEqual((pending['scope'], pending['run_id']), ('game', request['run_id']))
            contest.rulings.record('game', request['run_id'], seat, '按出生点裁定', operator='organizer')
        pending, = contest.rulings.pending()['pending']
        self.assertEqual((pending['scope'], pending['duel_id']), ('duel', duel))
        contest.rulings.record('duel', duel, 0, '按出生点裁定', operator='organizer')
        result = contest.duels.show(duel, manual=True)['result']
        self.assertEqual((result['outcome'], result['winner'], result['games_won']), ('win', 0, {'0': 1, '1': 1}))

    @case('L0-053', '手动对决允许基准 Bot 作为队伍的对手并安排同图换边')
    def test_manual_duel_with_baseline_opponent(self):
        contest = Contest(self.root / 'contest.sqlite')
        sides = [TEAMS[0], 'bot001']
        duel_id = contest.duels.accept_manual({'sides': sides, 'rules': 'prelim'})['duel_id']
        first, second = contest.runs.claim_submissions(2)
        self.assertEqual((first['duel_id'], second['duel_id']), (duel_id, duel_id))
        self.assertCountEqual(first['bots'], sides)
        self.assertEqual(second['bots'], first['bots'][::-1])
        self.assertEqual(first['config'], second['config'])

    @case('L0-054', '手动对决允许两侧均使用基准 Bot，也支持同一基准 Bot 自测')
    def test_manual_duel_between_baselines(self):
        contest = Contest(self.root / 'contest.sqlite')
        for sides in (['bot001', 'bot002'], ['bot003', 'bot003']):
            with self.subTest(sides=sides):
                duel_id = contest.duels.accept_manual({'sides': sides, 'rules': 'prelim'})['duel_id']
                self.assertEqual(contest.duels.show(duel_id, manual=True)['sides'], sides)
                first, second = contest.runs.claim_submissions(2)
                self.assertCountEqual(first['bots'], sides)
                self.assertEqual(second['bots'], first['bots'][::-1])

    @case('L0-006', '固定三张正式地图后重启，仍按原六局计划和局序结算')
    def test_formal_preliminary_duel_finishes_using_its_stored_plan(self):
        maps = sealed_maps('prelim')
        db = self.root / 'contest.sqlite'
        duel_id = 'duel-stored'
        plan_duel(Contest(db, {'prelim': maps}, phase='prelim'), duel_id, 'prelim')
        contest = Contest(db)
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        client = HttpClient(contest_handler(contest))
        duel_path = f'{DUELS_PATH}/{duel_id}'
        self.assertEqual(client.json(DUELS_PATH, {'sides': TEAMS}, method='POST', status=409)['code'], 'PHASE_BUSY')
        game_results = []
        for ordinal in range(6):
            self.assertTrue(worker.submit_next())
            request = arena.submitted[-1]
            run_id = request['run_id']
            self.assertEqual(request['bots'], TEAMS if ordinal % 2 == 0 else TEAMS[::-1])
            self.assertEqual((request['rules'], request['config']['map']), ('prelim', maps[ordinal // 2]))
            winner_seat = int(ordinal == 5)
            bots = [Bot(cmd, exit_after_reply=seat != winner_seat) for seat, cmd in enumerate(('U', 'D'))]
            _, manifest, _ = self.judge_archive(request, bots)
            game_results.append({'ordinal': ordinal + 1, 'run_id': run_id, 'winner': request['bots'][winner_seat],
                                 'score': dict(zip(request['bots'], manifest['result']['score']))})
            self.poll_and_review(arena, worker, run_id, 'finished', preliminary=True)
            if ordinal < 5:
                self.assertEqual(client.json(duel_path + '/status')['state'], 'in_progress')
        self.assertFalse(worker.submit_next())
        response = client.json(duel_path + '/result')
        self.assertEqual(response['side_results'], {'team001': 'win', 'team002': 'loss'})
        result = response['result']
        self.assertEqual(result['games_won'], dict(zip(TEAMS, (4, 2))))
        self.assertEqual(result['games_lost'], dict(zip(TEAMS, (2, 4))))
        self.assertEqual(result['games'], game_results)
        self.assertEqual(result['runs'], [game['run_id'] for game in game_results])
        self.assertEqual(HttpClient(contest_handler(Contest(db))).json(duel_path + '/result'), response)

    @case('L0-016', '天梯同图换边，双方各胜一局且无满局时可以裁定')
    def test_ladder_waits_for_ruling_if_early_wins_split(self):
        contest = Contest(self.root / 'ladder.sqlite')
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        duel_id = contest.duels.accept({'sides': TEAMS})['duel_id']
        run_ids = []
        for seat_winner in TEAMS:
            self.assertTrue(worker.submit_next())
            request = arena.submitted[-1]
            bots = [Bot(command, exit_after_reply=team != seat_winner)
                    for command, team in zip(('U', 'D'), request['bots'])]
            self.judge_archive(request, bots, started_at=STARTED)
            self.poll_and_review(arena, worker, request['run_id'], 'finished')
            self.assertIsNone(contest.duels.result(duel_id)['result'])
            run_ids.append(request['run_id'])
        first, second = arena.submitted
        self.assertEqual(first['config'], second['config'])
        self.assertEqual(first['bots'], second['bots'][::-1])
        pending, = contest.rulings.pending()['pending']
        self.assertEqual((pending['scope'], pending['duel_id']), ('duel', duel_id))
        contest.rulings.record('duel', duel_id, TEAMS[0], '核定天梯对决胜方', operator='organizer')
        result = contest.duels.result(duel_id)['result']
        self.assertEqual((result['outcome'], result['winner'], result['runs']), ('win', TEAMS[0], run_ids))
        self.assertEqual(result['games_won'], dict.fromkeys(TEAMS, 1))
        self.assertEqual(result['games_lost'], dict.fromkeys(TEAMS, 1))

    @case('L0-017', '正式与天梯构建单败或双败均按整场局数结算')
    def test_build_failures_settle_the_whole_duel(self):
        contest = Contest(self.root / 'builds.sqlite', {'prelim': sealed_maps('prelim')})
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        client = HttpClient(contest_handler(contest))
        for stage, ladder in (('prelim', True), ('prelim', False)):
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

    @case('L0-055', '手动对决因构建失败提前结算后，剩余执行显示未纳入结算')
    def test_settled_manual_duel_displays_unreviewed_sibling_as_waived(self):
        contest = Contest(self.root / 'contest.sqlite')
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        for sibling_status in ('accepted', 'finished'):
            with self.subTest(sibling_status=sibling_status):
                duel_id = contest.duels.accept_manual({'sides': TEAMS, 'rules': 'prelim'})['duel_id']
                self.assertTrue(worker.submit_next())
                self.assertTrue(worker.submit_next())
                first, second = arena.submitted[-2:]
                bots = [dict(bot, state=state) for bot, state in zip(self.metadata(first), ('failed', 'succeeded'))]
                arena_archive(self.root / 'runs' / first['run_id'], bots,
                              state='build_failed', duel_started_at=STARTED)
                arena.set_state(first['run_id'], 'build_failed')
                self.assertTrue(worker.poll_next())
                if sibling_status == 'finished':
                    self.judge_archive(second, started_at=STARTED)
                    arena.set_state(second['run_id'], 'finished')
                    self.assertTrue(worker.poll_next())

                before = contest.duels.show(duel_id, manual=True)
                sibling_before = before['attempts'][1]
                self.assertEqual((sibling_before['status'], sibling_before['review']), (sibling_status, None))
                self.assertTrue(worker.review_next())

                after = contest.duels.show(duel_id, manual=True)
                build_failure, sibling_after = after['attempts']
                self.assertEqual(after['state'], 'finished')
                self.assertEqual(after['result']['build_run_id'], first['run_id'])
                self.assertEqual((build_failure['status'], build_failure['review']), ('build_failed', 'build_failed'))
                self.assertEqual(sibling_after, dict(sibling_before, status='waived'))
                self.assertFalse(worker.poll_next())
                self.assertFalse(worker.review_next())

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
        self.assertIsNone(contest.duels.result(duel_id)['result'])

        self.assertTrue(worker.submit_next())
        second = arena.submitted[-1]
        directory = self.root / 'runs' / second['run_id']
        directory.mkdir(parents=True)
        bots = [dict(bot, state='failed') for bot in self.metadata(second)]
        arena_archive(directory, bots, state='build_failed', duel_started_at=STARTED)
        self.poll_and_review(arena, worker, second['run_id'], 'build_failed')
        self.assertIsNone(contest.duels.result(duel_id)['result'])

        evidence.write_text(dumps(manifest), encoding='utf-8')
        contest.runs.recheck(replacement)
        self.assertTrue(worker.review_next())
        self.assertEqual(contest.duels.show(duel_id)['attempts'][1]['review'], 'effective')
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

    @case('L0-056', '双方提交号均可省略，已记录的版本在换边后仍会核对')
    def test_optional_commits_are_checked_for_both_sides(self):
        contest = Contest(self.root / 'contest.sqlite')
        arena = ArenaStub()
        worker = ContestWorker(contest, self.root, arena)
        duel_id = contest.duels.accept({'sides': [TEAMS[0], 'bot002']})['duel_id']
        self.assertTrue(worker.submit_next())
        first = arena.submitted[-1]
        output, manifest, _ = self.judge_archive(first, [Bot('U'), Bot('D', exit_after_reply=True)],
                                                 started_at=STARTED)
        without_commits = [{'team_id': team} for team in first['bots']]
        unversioned = arena_archive(output, without_commits, duel_started_at=STARTED)
        self.assertEqual(review_archive(self.root, first, 'finished', ladder=True), unversioned)

        bots = [dict(bot) for bot in manifest['bots']]
        next(bot for bot in bots if bot['team_id'] == TEAMS[0]).pop('commit')
        arena_archive(output, bots, duel_started_at=STARTED)
        self.poll_and_review(arena, worker, first['run_id'], 'finished')
        self.assertEqual(contest.duels.show(duel_id)['attempts'][0]['review'], 'effective')

        self.assertTrue(worker.submit_next())
        second = arena.submitted[-1]
        self.assertEqual(second['bots'], first['bots'][::-1])
        output = self.root / 'runs' / second['run_id']
        bots = [dict(bot, state='failed') for bot in self.metadata(second)]
        expected = {bot['team_id']: bot['commit'] for bot in bots}
        baseline = next(bot for bot in bots if bot['team_id'] == 'bot002')
        baseline['commit'] = 'f' * 40
        arena_archive(output, bots, state='build_failed', duel_started_at=STARTED)
        self.poll_and_review(arena, worker, second['run_id'], 'build_failed')
        attempt = contest.duels.show(duel_id)['attempts'][1]
        self.assertEqual(attempt['review'], 'held')
        self.assertIn('提交版本', attempt['issue'])

        baseline.pop('commit')
        arena_archive(output, bots, state='build_failed', duel_started_at=STARTED)
        contest.runs.recheck(second['run_id'])
        self.assertTrue(worker.review_next())
        self.assertEqual(contest.duels.show(duel_id)['attempts'][1]['review'], 'build_failed')
        with contest.database.transaction(write=False) as connection:
            row = connection.execute('SELECT commits FROM duels WHERE duel_id = ?', (duel_id,)).fetchone()
        self.assertEqual(json.loads(row['commits']), expected)

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
        self.assertFalse(worker.submit_next())
        client = HttpClient(contest_handler(contest))
        expected = (
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
