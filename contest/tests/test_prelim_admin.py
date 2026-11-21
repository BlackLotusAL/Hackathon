import hashlib
from io import BytesIO
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .helpers import (ArenaStub, Bot, HttpClient, MatchFixture, admin_headers, arena_archive, case,
                      contest_handler, exported_tables, http_arena, plan_duel, roster_workbook, run_query_payload,
                      running_service, sealed_maps, wait_for, write_config, write_map_pools)

from service.arena import ArenaClient
from service.domain import ContestError
from service.server import Admin, Contest, ContestWorker
from service.workbook import DOC_REL, PKG_REL, SHEET_XML, TYPES


ROSTER = (('team002', '曝光'), ('team001', '微光'), ('team003', '领地 & 开发'))
BASE = '/admin/prelim'


class PrelimFixture(MatchFixture):
    def setUp(self):
        super().setUp()
        self.db = self.root / 'contest.sqlite'
        self.arena = ArenaStub()
        self.maps = {'prelim': sealed_maps('prelim')}
        self.open_phase('prelim')

    def open_phase(self, phase):
        self.contest = Contest(self.db, self.maps, phase=phase)
        self.worker = ContestWorker(self.contest, self.root, self.arena)
        admin = Admin(self.contest, 'organizer', 'test-password', self.arena)
        handler = contest_handler(self.contest, admin=admin)
        headers = admin_headers('organizer', 'test-password')
        self.admin = HttpClient(handler, prefix=BASE, headers=headers)
        self.manager = HttpClient(handler, prefix='/admin', headers=headers)
        self.client = HttpClient(handler)

    def next_run(self):
        count = len(self.arena.submitted)
        self.assertTrue(self.worker.submit_next())
        self.assertEqual(len(self.arena.submitted), count + 1)
        return self.arena.submitted[-1]

    def poll_result(self, run_id, state, **fields):
        self.arena.set_state(run_id, state, **fields)
        self.worker.poll_preliminary_round()
        self.assertIn(run_id, self.arena.queries[-1])

    def build_both_failed(self, request):
        output = self.root / 'runs' / request['run_id']
        bots = [dict(bot, state='failed') for bot in self.metadata(request)]
        arena_archive(output, bots, state='build_failed')
        self.poll_result(request['run_id'], 'build_failed')
        self.assertTrue(self.worker.review_next())


class PrelimAdminTests(PrelimFixture):
    @case('L0-026', '后台裁定复核通过的未决胜单局和整场，正式初赛可确认导出')
    def test_rulings_complete_the_official_batch_without_changing_evidence(self):
        self.admin.json('roster', roster_workbook(ROSTER[:2]), method='POST')
        self.admin.json('plan', method='POST')
        games = []
        for ordinal in range(1, 7):
            request = self.next_run()
            output, _, _ = self.judge_archive(request)
            evidence = [(output / name).read_bytes() for name in ('match.json', 'replay.json')]
            run_id = request['run_id']
            self.poll_result(run_id, 'finished')
            self.assertTrue(self.worker.review_next())
            self.assertFalse(self.worker.submit_next())
            pending, = self.manager.json('rulings')['pending']
            self.assertEqual((pending['scope'], pending['run_id'], pending['ordinal'], pending['batch_id']),
                             ('game', run_id, ordinal, 'official'))
            winner = request['bots'][0]
            decision = {'scope': 'game', 'target': run_id, 'winner': winner, 'reason': '按公示规则核定单局胜方'}
            if ordinal == 1:
                self.assertEqual(self.manager.post('rulings', decision, headers={'X-Contest-Admin': ''})[0], 403)
                self.assertEqual(self.manager.json('rulings', dict(decision, winner='team099'),
                                                   method='POST', status=400)['code'], 'INVALID_RULING_WINNER')
                self.assertEqual(self.manager.json('rulings', dict(decision, reason='无效\u0001文本'),
                                                   method='POST', status=400)['code'], 'INVALID_RULING')
            accepted = self.manager.json('rulings', decision, method='POST')
            self.assertEqual(accepted['operator'], 'organizer')
            self.assertEqual(self.manager.json('rulings', decision, method='POST'), accepted)
            self.assertEqual(self.manager.json('rulings', dict(decision, reason='其他理由'),
                                               method='POST', status=409)['code'], 'RULING_CONFLICT')
            self.assertEqual([(output / name).read_bytes() for name in ('match.json', 'replay.json')], evidence)
            games.append((run_id, winner))

        duel_id = request['duel_id']
        pending, = self.manager.json('rulings')['pending']
        self.assertEqual((pending['scope'], pending['duel_id']), ('duel', duel_id))
        self.assertEqual(self.admin.json('confirm', method='POST', status=409)['code'], 'PRELIM_INCOMPLETE')
        reason = '按公示规则核定整场胜方'
        self.manager.json('rulings', {'scope': 'duel', 'target': duel_id, 'winner': ROSTER[0][0], 'reason': reason},
                          method='POST')
        self.assertEqual(self.manager.json('rulings')['pending'], [])
        result = self.client.json(f'/contest/v1/duels/{duel_id}/result')['result']
        self.assertEqual((result['winner'], result['reason']), (ROSTER[0][0], reason))
        self.assertEqual([(game['run_id'], game['winner']) for game in result['games']], games)
        self.assertTrue(all(game['reason'] == '按公示规则核定单局胜方' for game in result['games']))
        self.assertEqual(result['games_won'], dict.fromkeys((team for team, _ in ROSTER[:2]), 3))
        self.assertEqual(self.admin.json('confirm', method='POST')['state'], 'confirmed')
        code, workbook = self.admin.request('results.xlsx')
        self.assertEqual(code, 200)
        tables = exported_tables(workbook)
        self.assertIn(f'人工裁定（{reason}）', tables['对决'][1])
        self.assertEqual(tables['对局'][1:], [
            [duel_id, ordinal, run_id, winner, '按公示规则核定单局胜方']
            for ordinal, (run_id, winner) in enumerate(games, 1)
        ])

    @case('L0-027', '独立压测按真实单循环编排、查询、重跑并与正式排名和导出隔离')
    def test_load_test_batches_share_arena_and_isolate_all_official_results(self):
        first, second = 'load-tests/load-first', 'load-tests/load-second'
        workbook = roster_workbook(ROSTER)
        self.assertEqual(self.client.post(f'/admin/{first}', workbook)[0], 401)
        self.assertEqual(self.manager.post('load-tests/official', workbook)[0], 400)
        self.assertEqual(self.manager.post(first, roster_workbook(ROSTER[:1]))[0], 400)
        created = self.manager.json(first, workbook, method='POST')
        self.assertEqual((created['state'], created['duels']['total'], created['games']['total']), ('running', 3, 18))
        self.assertEqual(self.manager.json(first, workbook, method='POST')['duels'], created['duels'])
        self.assertEqual(self.manager.json(first, roster_workbook(ROSTER[:2]), method='POST', status=409)['code'],
                         'LOAD_TEST_CONFLICT')
        self.assertEqual(self.admin.json('progress')['state'], 'empty')
        self.manager.json(second, roster_workbook(ROSTER[:2]), method='POST')
        self.assertCountEqual([item['batch_id'] for item in self.manager.json('load-tests')['batches']],
                              ['load-first', 'load-second'])
        self.open_phase('prelim')
        self.admin.json('roster', roster_workbook(ROSTER[:2]), method='POST')
        self.assertEqual(self.admin.json('plan', method='POST', status=409)['code'], 'PHASE_BUSY')
        requests = [self.next_run() for _ in range(4)]
        self.assertEqual(len({request['run_id'] for request in requests}), 4)
        self.assertTrue(all(request['rules'] == 'prelim' and 'file' in request['config']['map']
                            for request in requests))
        retried = requests[0]
        self.poll_result(retried['run_id'], 'platform_error', reason='wall_timeout')
        self.assertTrue(self.worker.review_next())
        self.assertEqual(self.admin.post('retry', {'run_id': retried['run_id']})[0], 404)
        self.assertEqual(self.manager.post(second + '/retry', {'run_id': retried['run_id']})[0], 404)
        retry = self.manager.json(first + '/retry', {'run_id': retried['run_id']}, method='POST')
        replayed = self.next_run()
        self.assertEqual(replayed, dict(retried, run_id=retry['retry_run_id']))
        for request in [replayed, *requests[1:]]:
            self.build_both_failed(request)
            for resource in ('status', 'result'):
                self.assertEqual(self.client.request(f"/contest/v1/duels/{request['duel_id']}/{resource}")[0], 404)
        completed = self.manager.json(first)
        self.assertEqual((completed['state'], completed['duels']['settled'], completed['attempts']),
                         ('finished', 3, {'retries': 1, 'voided': 1}))
        self.assertTrue(completed['finished_at'])
        self.assertGreater(completed['duels_per_minute'], 0)
        self.assertEqual(self.manager.json(first + '/stop', method='POST'), completed)
        self.assertEqual(self.manager.json(second)['duels']['settled'], 1)
        self.assertEqual(self.admin.json('progress')['duels']['settled'], 0)
        self.admin.json('plan', method='POST')
        self.assertEqual(self.manager.json('load-tests/load-third', workbook, method='POST', status=409)['code'],
                         'PHASE_BUSY')
        self.assertEqual(self.admin.json('standings', status=409)['code'], 'PRELIM_INCOMPLETE')
        official = self.next_run()
        self.build_both_failed(official)
        self.assertEqual(self.admin.json('confirm', method='POST')['state'], 'confirmed')
        code, workbook = self.admin.request('results.xlsx')
        self.assertEqual(code, 200)
        tables = exported_tables(workbook)
        self.assertEqual(len(tables['对决']), 2)
        self.assertEqual(tables['对决'][1][0], official['duel_id'])

    @case('L0-029', '压测拒单后仍隔离赛程，结束批次后保留结果并关闭未结算任务')
    def test_load_test_rejection_and_stop_leave_formal_scheduling_clear(self):
        rejected = 'load-tests/load-rejected'
        self.manager.json(rejected, roster_workbook(ROSTER[:2]), method='POST')
        with patch.object(self.arena, 'submit', return_value=409) as submit:
            self.assertTrue(self.worker.submit_next())
        refused = submit.call_args.args[0]
        self.assertEqual(self.manager.json(rejected)['attention'][0]['run_id'], refused['run_id'])
        self.assertEqual(self.contest.runs.preliminary_poll_targets(), [])
        reopened = Contest(self.db)
        sides = {'sides': ['team001', 'team002']}
        with self.assertRaises(ContestError) as error:
            reopened.duels.accept(sides)
        self.assertEqual(error.exception.code, 'PHASE_BUSY')
        self.assertIsNone(reopened.runs.claim())
        self.assertEqual(self.manager.json(rejected + '/stop', method='POST')['state'], 'stopped')

        batch = 'load-tests/load-with-results'
        self.manager.json(batch, roster_workbook(ROSTER), method='POST')
        completed = self.next_run()
        self.build_both_failed(completed)
        confirmed = self.contest.duels.show(completed['duel_id'])['result']
        run_id = self.next_run()['run_id']
        claims = self.contest.runs.claim_preliminary_polls([run_id])
        self.assertEqual([claim.run_id for claim in claims], [run_id])
        self.assertEqual(self.client.post(f'/admin/{batch}/stop')[0], 401)
        self.assertEqual(self.manager.post('load-tests/load-unknown/stop')[0], 404)
        stopped = self.manager.json(batch + '/stop', method='POST')
        self.assertEqual(stopped['state'], 'stopped')
        self.assertIsNone(stopped['finished_at'])
        self.assertTrue(stopped['stopped_at'])
        self.assertEqual(stopped['duels'], {'total': 3, 'settled': 1, 'awaiting_action': 0, 'pending': 0, 'cancelled': 2})
        self.assertEqual(stopped['games']['cancelled'], 12)
        self.assertEqual(stopped['attention'], [])
        self.contest.runs.complete_polls(claims, 200, run_query_payload({'run_id': run_id, 'state': 'finished'}))
        self.assertFalse(self.worker.review_next())
        self.assertFalse(self.worker.submit_next())
        self.assertEqual(self.contest.runs.preliminary_poll_targets(), [])
        self.assertEqual(self.contest.duels.show(completed['duel_id'])['result'], confirmed)
        self.assertEqual(self.manager.json(batch + '/stop', method='POST'), stopped)
        self.assertEqual(reopened.preliminary.load_test_progress('load-with-results'), stopped)
        with self.assertRaises(ContestError) as error:
            reopened.preliminary.retry(run_id, load_test_id='load-with-results')
        self.assertEqual(error.exception.code, 'ATTEMPT_CLOSED')
        with self.assertRaises(ContestError) as error:
            reopened.preliminary.start_load_test('load-wrong-phase', roster_workbook(ROSTER))
        self.assertEqual(error.exception.code, 'PHASE_INACTIVE')
        duel_id = reopened.duels.accept(sides)['duel_id']
        self.assertEqual(reopened.runs.claim()['duel_id'], duel_id)

    @case('L0-025', '初赛以 HTTP 批量查询当前尝试，每批最多 100 局，按编号合并结果')
    def test_preliminary_round_filters_and_batches_run_ids(self):
        roster = roster_workbook([(f'team{number:03d}', '') for number in range(1, 17)])
        self.admin.json('roster', roster, method='POST')
        self.admin.json('plan', method='POST')
        runs = self.contest.runs
        unconfirmed = runs.claim()
        runs.complete_submission(unconfirmed['run_id'], None)
        submitted = [self.next_run() for _ in range(103)]
        missing, finished, replaced, soon_finished = submitted[:4]
        del self.arena.states[missing['run_id']]

        claims = runs.claim_preliminary_polls([finished['run_id']])
        runs.complete_polls(claims, 200, run_query_payload({'run_id': finished['run_id'], 'state': 'finished'}))
        stale = runs.claim_preliminary_polls([replaced['run_id']])
        runs.invalidate(replaced['run_id'], '测试平台故障')
        replacement_id = runs.retry(replaced['run_id'])
        self.assertEqual(self.next_run()['run_id'], replacement_id)
        runs.complete_polls(stale, 200, run_query_payload({'run_id': replaced['run_id'], 'state': 'finished'}))
        sending = runs.claim()
        self.arena.set_state(soon_finished['run_id'], 'finished')

        self.assertFalse(self.worker.poll_next())
        with http_arena(None) as (url, calls, peer):
            peer.states.update(self.arena.states)
            ContestWorker(self.contest, self.root, ArenaClient(url)).poll_preliminary_round()
        self.assertEqual([len(batch) for batch in peer.queries], [100, 3])
        for batch in peer.queries:
            request = calls.get_nowait()
            self.assertEqual((request['method'], request['path']), ('POST', '/arena/v1/runs/query'))
            self.assertEqual(request['headers']['content-type'], 'application/json')
            self.assertEqual(request['body'], {'run_ids': batch})
        self.assertTrue(calls.empty())
        queried = [run_id for batch in peer.queries for run_id in batch]
        expected = {run['run_id'] for run in submitted} - {finished['run_id'], replaced['run_id']}
        expected.update((unconfirmed['run_id'], replacement_id))
        self.assertCountEqual(queried, expected)
        self.assertNotIn(sending['run_id'], queried)

        def first_run(request):
            return self.contest.duels.show(request['duel_id'])['attempts'][0]

        self.assertEqual(first_run(unconfirmed)['submission'], 'unconfirmed')
        self.assertIn('未找到已受理', first_run(missing)['issue'])
        self.assertIsNone(first_run(replaced)['arena_result'])
        self.assertEqual(first_run(soon_finished)['arena_result'], {'state': 'finished'})
        self.worker.poll_preliminary_round()
        self.assertCountEqual([run_id for batch in self.arena.queries for run_id in batch],
                              expected - {soon_finished['run_id']})

    @case('L1-007', '初赛每 10 秒轮询一轮，上一轮超时时跳过该定时点')
    def test_preliminary_rounds_keep_cadence_when_a_batch_fails(self):
        self.admin.json('roster', roster_workbook(ROSTER), method='POST')
        self.admin.json('plan', method='POST')
        running, queued = self.next_run()['run_id'], self.next_run()['run_id']
        self.arena.set_state(running, 'running')

        class Clock:
            now = 0

            def is_set(self):
                return self.now >= 40

            def wait(self, delay):
                self.now += delay
                return self.is_set()

        clock = Clock()
        queries = []
        query = self.arena.query_runs

        def reply(run_ids):
            queries.append((clock.now, run_ids))
            if len(queries) == 1:
                clock.now += 16
                return 503, None
            clock.now += 3
            return query(run_ids)

        with patch('service.server.monotonic', side_effect=lambda: clock.now):
            with patch.object(self.arena, 'query_runs', side_effect=reply):
                self.worker.poll_preliminary(clock)
        self.assertEqual(queries, [(0, [running, queued]), (20, [running, queued]), (30, [running, queued])])
        self.assertEqual(self.contest.runs.preliminary_poll_targets(), [running, queued])

    @case('L0-009', '初赛名单到三表导出包含构建双败与故障重跑')
    def test_three_team_round_robin_progress_and_three_result_sheets(self):
        self.commits = {team: hashlib.sha256(team.encode('utf-8')).hexdigest() for team, _ in ROSTER}
        self.arena.summary = {'at': '2026-11-01T14:05:00+08:00', 'slots': {'idle': 1, 'busy': 0, 'disabled': 0},
                              'queue': 0, 'done_10m': 0, 'platform_errors_1h': 0}
        self.assertEqual(self.client.json('/admin/arena/status', headers=self.admin.headers), self.arena.summary)
        roster = roster_workbook(ROSTER)
        data = self.admin.json('roster', roster, method='POST')
        self.assertEqual([(row['team_id'], row['name']) for row in data['teams']], list(ROSTER))
        self.assertEqual(data['state'], 'roster_imported')
        data = self.admin.json('plan', method='POST')
        self.assertEqual((data['duels']['total'], data['games']['total']), (3, 18))
        self.assertEqual(self.admin.json('plan', method='POST'), data)
        self.admin.json('confirm', method='POST', status=409)

        build = self.next_run()
        self.assertEqual(set(build['bots']), {'team001', 'team002'})
        self.build_both_failed(build)
        data = self.admin.json('progress')
        self.assertEqual((data['duels']['settled'], data['games']['effective'], data['games']['waived']), (1, 0, 6))

        original = self.next_run()
        self.assertEqual(self.admin.json('progress')['games']['waiting_result'], 1)
        self.poll_result(original['run_id'], 'platform_error', reason='slot_unhealthy')
        self.assertEqual(self.admin.json('progress')['games']['waiting_review'], 1)
        self.assertTrue(self.worker.review_next())
        data = self.admin.json('progress')
        self.assertEqual((data['duels']['awaiting_action'], data['games']['awaiting_action']), (1, 1))
        self.assertEqual(data['attempts'], {'retries': 0, 'voided': 1})

        retry_id = self.contest.runs.retry(original['run_id'])
        queries_before = len(self.arena.queries)
        self.worker.poll_preliminary_round()
        self.assertEqual(len(self.arena.queries), queries_before)
        replacement = self.next_run()
        self.assertEqual(replacement, dict(original, run_id=retry_id))
        self.assertEqual(self.admin.json('progress')['games']['awaiting_action'], 0)

        ran = {}
        for index in range(12):
            request = replacement if index == 0 else self.next_run()
            duel_id, sides = request['duel_id'], request['bots']
            played = ran.setdefault(duel_id, [])
            if set(sides) == {'team002', 'team003'}:
                winner = 'team003' if len(played) < 4 else 'team002'
            else:
                self.assertEqual(set(sides), {'team001', 'team003'})
                winner = 'team001' if len(played) < 5 else 'team003'
            bots = [Bot(command, exit_after_reply=team != winner) for command, team in zip(('U', 'D'), sides)]
            _, manifest, _ = self.judge_archive(request, bots)
            self.assertEqual([bot['commit'] for bot in manifest['bots']], [self.commits[team] for team in sides])
            self.poll_result(request['run_id'], 'finished')
            self.assertTrue(self.worker.review_next())
            played.append((request['run_id'], winner))
            if index == 0:
                data = self.admin.json('progress')
                self.assertEqual(data['games']['effective'], 1)
                self.assertEqual(data['attempts'], {'retries': 1, 'voided': 1})

        self.assertEqual(len(ran), 2)
        self.assertFalse(self.worker.submit_next())
        plan_duel(self.contest, 'duel-unrelated', 'prelim')
        data = self.admin.json('progress')
        self.assertEqual(data['state'], 'awaiting_confirmation')
        self.assertEqual(data['duels'], {'total': 3, 'settled': 3, 'awaiting_action': 0, 'pending': 0})
        self.assertEqual((data['games']['total'], data['games']['effective'], data['games']['waived']), (18, 12, 6))
        self.assertEqual(data['attempts'], {'retries': 1, 'voided': 1})
        ranking = [[1, 'team001', 5, 7], [2, 'team003', 5, 7], [3, 'team002', 2, 10]]
        self.assertEqual(self.admin.json('standings')['rows'], ranking)
        self.admin.json('results.xlsx', status=409)
        data = self.admin.json('confirm', method='POST')
        self.assertEqual(data['state'], 'confirmed')
        self.assertEqual(self.admin.json('confirm', method='POST')['confirmed_at'], data['confirmed_at'])

        status, workbook = self.admin.request('results.xlsx')
        self.assertEqual(status, 200)
        tables = exported_tables(workbook)
        self.assertEqual(list(tables), ['排名', '对决', '对局'])
        self.assertEqual(tables['排名'], [['名次', '队伍编号', '胜局数', '负局数'], *ranking])
        duels = {row[0]: row for row in tables['对决'][1:]}
        self.assertEqual(set(duels), {build['duel_id'], *ran})
        self.assertEqual(duels[build['duel_id']][3:], [0, 6, 0, 6, 'double_loss', None, '构建双败', build['run_id']])
        self.assertEqual(tables['对局'][0], ['duel_id', '预定局序', 'run_id', '胜方', '裁定理由'])
        self.assertEqual(tables['对局'][1:], [[duel_id, ordinal, run_id, winner, None] for duel_id, runs in ran.items()
                                              for ordinal, (run_id, winner) in enumerate(runs, 1)])

    @case('L1-004', '名单只使用约定表头，校验队伍编号和重复行')
    def test_roster_corrections_validate_team_ids(self):
        rows = [('微光', 'team001'), ('曝光', 'team002')]
        aliases = roster_workbook(rows, ('team_name', 'team_id'))
        self.assertEqual(self.admin.json('roster', aliases, method='POST', status=400)['code'], 'INVALID_ROSTER')
        book = roster_workbook(rows, ('队名', '队伍编号'))
        self.admin.json('roster', book, method='POST')
        for team in ('t001', 'team01', 'team0001', 'bot001'):
            with self.subTest(team=team):
                error = self.admin.json('roster', roster_workbook([(team, '')]), method='POST', status=400)
                self.assertIn('team 加三位数字', error['message'])
        error = self.admin.json('roster', roster_workbook([ROSTER[0]] * 2), method='POST', status=400)
        self.assertIn('重复', error['message'])
        self.assertEqual(len(self.admin.json('progress')['teams']), 2)
        self.admin.json('roster', roster_workbook(ROSTER), method='POST')

    @case('L0-019', '编排后名单冻结且重复导入不能产生额外赛程')
    def test_planned_roster_is_immutable(self):
        self.admin.json('plan', method='POST', status=409)
        self.admin.json('roster', roster_workbook(ROSTER), method='POST')
        self.admin.json('plan', method='POST')
        before = self.admin.json('progress')
        changed = roster_workbook([('team001', '变更后的队名'), ('team002', '曝光')])
        self.assertEqual(self.admin.json('roster', changed, method='POST', status=409)['code'], 'ROSTER_FROZEN')
        self.assertEqual(self.admin.json('roster', roster_workbook(ROSTER), method='POST'), before)
        self.assertEqual(self.admin.json('plan', method='POST'), before)
        self.assertEqual(self.admin.json('progress'), before)

    @case('L1-005', 'Excel 共享字符串和稀疏单元格可读取')
    def test_imports_excel_shared_strings_and_sparse_cells(self):
        ns = f'{{{SHEET_XML}}}'
        output = BytesIO()
        with ZipFile(BytesIO(roster_workbook([('team007', None), ('team008', '微光')]))) as source, ZipFile(output, 'w') as book:
            sheet_path, rel_path = 'xl/worksheets/sheet1.xml', 'xl/_rels/workbook.xml.rels'
            sheet = ET.fromstring(source.read(sheet_path))
            shared = ET.Element(ns + 'sst')
            for index, cell in enumerate(sheet.iter(ns + 'c')):
                item = ET.SubElement(shared, ns + 'si')
                inline = cell.find(ns + 'is')
                item.append(inline.find(ns + 't'))
                cell.remove(inline)
                cell.set('t', 's')
                ET.SubElement(cell, ns + 'v').text = str(index)
            relationships = ET.fromstring(source.read(rel_path))
            ET.SubElement(relationships, f'{{{PKG_REL}}}Relationship', Id='rId9',
                          Type=DOC_REL + '/sharedStrings', Target='/xl/sharedStrings.xml')
            types = ET.fromstring(source.read('[Content_Types].xml'))
            ET.SubElement(types, f'{{{TYPES}}}Override', PartName='/xl/sharedStrings.xml',
                          ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml')
            for filename in ('_rels/.rels', 'xl/workbook.xml'):
                book.writestr(filename, source.read(filename))
            for filename, value in ((sheet_path, sheet), (rel_path, relationships),
                                    ('[Content_Types].xml', types), ('xl/sharedStrings.xml', shared)):
                book.writestr(filename, ET.tostring(value, encoding='utf-8'))

        data = self.admin.json('roster', output.getvalue(), method='POST')
        self.assertEqual([(team['team_id'], team['name']) for team in data['teams']], [('team007', ''), ('team008', '微光')])

    @case('L0-010', '退赛保留既有结果并处理尚未开打的对决')
    def test_withdrawal_keeps_completed_results_and_settles_unsent_duels(self):
        self.admin.json('roster', roster_workbook(ROSTER), method='POST')
        self.admin.json('plan', method='POST')
        first = self.next_run()
        self.build_both_failed(first)
        before = self.contest.duels.show(first['duel_id'])['result']
        active = self.next_run()
        self.assertEqual(set(active['bots']), {'team002', 'team003'})
        data = self.admin.json('withdraw', {'teams': ['team001', 'team002']}, method='POST')
        self.assertEqual(data['duels']['settled'], 2)
        self.assertEqual(data['games']['waived'], 12)
        self.assertEqual(data['games']['waiting_result'], 1)
        self.assertEqual({team['team_id'] for team in data['teams'] if team['withdrawn']}, {'team001', 'team002'})
        self.assertEqual(self.contest.duels.show(first['duel_id'])['result'], before)
        self.assertIsNone(self.contest.duels.show(active['duel_id'])['result'])


class PrelimHTTPTests(MatchFixture):
    def _next_submission(self, received):
        event = wait_for(lambda: received.get(timeout=20),
                         lambda event: (event['method'], event['path']) == ('POST', '/arena/v1/runs'))
        return event['body']

    def _report_finished(self, arena, archive, request):
        winner = min(request['bots'])
        bots = [Bot(command, exit_after_reply=team != winner)
                for command, team in zip(('U', 'D'), request['bots'])]
        self.judge_archive(request, bots, archive_root=archive)
        arena.set_state(request['run_id'], 'finished')

    @case('L0-011', '通过 HTTP 验证初赛重启重跑后，导出和 Portal 查询引用同一生效执行')
    def test_preliminary_retry_after_restart_is_reported_in_export_and_portal(self):
        db = self.root / 'var/contest.sqlite'
        archive = self.root / 'archives'
        pools = write_map_pools(self.root / 'config/map-pools.json', 'prelim')
        with http_arena(None) as (arena_url, received, arena):
            settings = dict(db=str(db), archive_root=str(archive), arena_url=arena_url,
                            maps=pools.name, host='127.0.0.1', port=0)
            config = write_config(pools.with_name('contest.json'), **settings, phase='prelim')
            with running_service(config) as address:
                admin = HttpClient(address, prefix=BASE, headers=admin_headers())
                admin.json('roster', roster_workbook(ROSTER[:2]), method='POST')
                admin.json('plan', method='POST')

                original = self._next_submission(received)
                self.assertEqual(original['run_id'], 'run-1.1')
                arena.set_state(original['run_id'], 'platform_error', reason='slot_unhealthy')
                paused = wait_for(lambda: admin.json('progress'),
                                  lambda info: info['attempts']['voided'] == 1)
                self.assertEqual(paused['attention'], [{
                    'duel_id': original['duel_id'], 'run_id': 'run-1.1', 'ordinal': 1,
                    'issue': 'slot_unhealthy', 'retryable': True,
                }])
            # The stored preliminary plan must drain even after the configured phase changes.
            write_config(config, **settings, phase='final-ladder')
            with running_service(config) as address:
                admin = HttpClient(address, prefix=BASE, headers=admin_headers())
                self.assertEqual(admin.json('progress'), paused)

                retry = admin.json('retry', {'run_id': original['run_id']}, method='POST')
                self.assertEqual(retry['retry_run_id'], 'run-1.2')
                admin.json('retry', {'run_id': original['run_id']}, method='POST', status=409)
                replacement = self._next_submission(received)
                self.assertEqual(replacement, dict(original, run_id='run-1.2'))
                self._report_finished(arena, archive, replacement)
                for _ in range(5):
                    self._report_finished(arena, archive, self._next_submission(received))

                settled = wait_for(lambda: admin.json('progress'), lambda info: info['duels']['settled'] == 1)
                self.assertEqual(settled['games']['effective'], 6)
                self.assertEqual(settled['attempts'], {'voided': 1, 'retries': 1})
                self.assertEqual(settled['attention'], [])
                self.assertEqual(admin.json('confirm', method='POST')['state'], 'confirmed')

                code, workbook = admin.request('results.xlsx')
                self.assertEqual(code, 200)
                rows = exported_tables(workbook)['对局'][1:]
                portal = HttpClient(address)
                duel_id = original['duel_id']
                result = portal.json(f'/contest/v1/duels/{duel_id}/result')['result']
                self.assertEqual(result['runs'], ['run-1.2', *(f'run-{number}.1' for number in range(2, 7))])
                self.assertEqual(rows, [
                    [duel_id, game['ordinal'], game['run_id'], game['winner'], None] for game in result['games']
                ])
