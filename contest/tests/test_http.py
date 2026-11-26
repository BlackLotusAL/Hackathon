"""Exercise Portal, Arena and organizer HTTP contracts."""
from queue import Empty, Queue
from threading import Event
from unittest.mock import patch

from service.server import Contest

from .helpers import (TEAMS, Bot, HttpClient, MatchFixture, admin_headers, arena_archive,
                      case, contest_handler, http_arena, roster_workbook, run_query_payload,
                      plan_duel, running_service, wait_for, write_config, write_map_pools)


class HttpServiceTests(MatchFixture):
    @case('L0-037', '三队批次并发下发同场多局，并按 Arena 空闲槽位补位')
    def test_submits_twelve_games_from_three_duels(self):
        database = self.root / 'contest.sqlite'
        pools = write_map_pools(self.root / 'maps.json', 'prelim')
        path = '/admin/load-tests/load-slots'
        limited = {'slots': {'idle': 4, 'busy': 8, 'disabled': 0}, 'queue': 2}
        with http_arena(limited) as (arena_url, _, arena):
            config = write_config(self.root / 'contest.json', maps=pools.name, db=str(database),
                                  host='127.0.0.1', port=0, archive_root=str(self.root / 'archives'),
                                  arena_url=arena_url)
            arrivals = Queue()
            release = Event()
            submit = arena.submit

            def hold_response(request):
                arrivals.put(request)
                return submit(request) if release.wait(timeout=5) else 503

            with patch.object(arena, 'submit', hold_response), running_service(config) as address:
                admin = HttpClient(address, headers=admin_headers())
                batch = admin.json(path, {'range': ['team001', 'team003'], 'rules': 'prelim'}, method='POST')
                self.assertEqual((batch['duels']['total'], batch['games']['total']), (3, 18))
                try:
                    requests = [arrivals.get(timeout=4) for _ in range(2)]
                    self.assertNotEqual(requests[0]['duel_id'], requests[1]['duel_id'])
                    with self.assertRaises(Empty):
                        arrivals.get(timeout=0.6)

                    arena.summary = {'slots': {'idle': 12, 'busy': 0, 'disabled': 0}, 'queue': 0}
                    requests.extend(arrivals.get(timeout=4) for _ in range(10))
                    duel_ids = {request['duel_id'] for request in requests}
                    run_ids = {request['run_id'] for request in requests}
                    self.assertEqual(len(duel_ids), 3)
                    self.assertEqual(len(run_ids), 12)
                    arena.summary = {'slots': {'idle': 0, 'busy': 12, 'disabled': 0}, 'queue': 0}
                finally:
                    release.set()
                wait_for(lambda: len(arena.submitted), lambda count: count == 12)
                with self.assertRaises(Empty):
                    arrivals.get(timeout=0.6)

                arena.set_state(requests[0]['run_id'], 'platform_error', reason='wall_timeout')
                arena.summary = {'slots': {'idle': 1, 'busy': 11, 'disabled': 0}, 'queue': 0}
                next_request = arrivals.get(timeout=4)
                self.assertIn(next_request['duel_id'], duel_ids)
                self.assertNotIn(next_request['run_id'], run_ids)
                wait_for(lambda: len(arena.submitted), lambda count: count == 13)

    @case('L0-035', '手动对决使用最新初赛规则，支持同队模拟及后台回放')
    def test_manual_duels_reach_arena_with_current_rules(self):
        database = self.root / 'contest.sqlite'
        archive = self.root / 'archive'
        with http_arena(200) as (arena_url, received, arena):
            config = write_config(
                self.root / 'contest.json', db=str(database), host='127.0.0.1', arena_url=arena_url,
                archive_root=str(archive), phase='prelim', port=0,
            )
            with running_service(config) as url:
                client = HttpClient(url)
                admin = HttpClient(url, headers=admin_headers())
                path = '/admin/duels'
                self.assertEqual(client.post(path, {})[0], 401)
                self.assertEqual(admin.post(path, {'sides': TEAMS, 'rules': 'final'})[0], 400)
                check = {'target': 'http://127.0.0.1:1', 'rules': 'final'}
                self.assertEqual(admin.json('/admin/check', check, method='POST', status=400)['code'],
                                 'INVALID_RULES')
                self.assertEqual(admin.post(path, {'sides': [TEAMS[0], 'bot099'], 'rules': 'prelim'})[0], 400)
                self.assertEqual(admin.request('/admin/replay.js')[0], 200)
                status, page = admin.request('/admin')
                self.assertEqual(status, 200)
                self.assertNotIn(b'id="manual-stop"', page)
                body = {'sides': [TEAMS[0], TEAMS[0]], 'rules': 'prelim'}
                duel_id = admin.json(path, body, method='POST', status=202)['duel_id']
                detail_path = f'{path}/{duel_id}'
                detail = admin.json(detail_path)
                self.assertEqual((detail['rules'], detail['sides'], len(detail['attempts'])),
                                 ('prelim', body['sides'], 2))
                self.assertEqual(client.request(f'/contest/v1/duels/{duel_id}/result')[0], 404)

                requests = []
                while len(requests) < 2:
                    event = received.get(timeout=5)
                    if event['path'] == '/arena/v1/runs':
                        requests.append(event['body'])
                first, second = requests
                self.assertEqual((first['duel_id'], first['rules']), (duel_id, 'prelim'))
                self.assertEqual(first['bots'], body['sides'])
                self.assertEqual(set(first['config']['map']), {'seed'})
                self.assertEqual(second, dict(first, run_id=second['run_id']))

                started = '2026-11-19T15:05:00+08:00'
                replay_paths = [f"/admin/runs/{request['run_id']}/replay" for request in requests]
                self.assertEqual(admin.request(replay_paths[0])[0], 404)
                _, _, winning = self.judge_archive(second, [Bot('U'), Bot('D', exit_after_reply=True)],
                                                   started_at=started, archive_root=archive)
                arena.set_state(second['run_id'], 'finished')
                wait_for(lambda: admin.request(replay_paths[1])[0], lambda status: status == 200)
                self.assertEqual(admin.json(detail_path)['state'], 'in_progress')
                _, _, both_lost = self.judge_archive(first, started_at=started, archive_root=archive)
                self.assertEqual(both_lost['result']['reason'], 'double_loss')
                arena.set_state(first['run_id'], 'finished')
                settled = wait_for(lambda: admin.json(detail_path), lambda duel: duel['state'] == 'finished')
                self.assertEqual(settled['result']['runs'], [request['run_id'] for request in requests])
                self.assertEqual(settled['result']['winner'], 0)
                self.assertEqual(settled['result']['games_won'], {'0': 1, '1': 0})
                self.assertTrue(all(run['has_replay'] for run in settled['attempts']))
                self.assertEqual(client.request(replay_paths[0])[0], 401)
                self.assertEqual(admin.json(replay_paths[0]), both_lost)
                self.assertEqual(admin.json(replay_paths[1]), winning)
                self.assertEqual(admin.json('/admin/rulings')['pending'], [])
                self.assertEqual([row['duel_id'] for row in admin.json(path)['duels']], [duel_id])

                active_id = admin.json(path, body, method='POST', status=202)['duel_id']
                active_path = f'{path}/{active_id}'
                self.assertEqual(admin.json(f'{active_path}/stop', method='POST', status=404)['code'], 'NOT_FOUND')
                self.assertEqual(admin.json(active_path)['state'], 'in_progress')
                self.assertEqual(admin.json('/admin/prelim/progress')['state'], 'empty')
                found = Contest(database).duels.ladder_submissions(TEAMS, '1970-01-01T00:00:00+08:00')
                self.assertEqual(found['duels'], [])

    @case('L0-030', '旧赛段配置和待执行记录不会进入当前赛程')
    def test_retired_stage_does_not_enter_current_schedule(self):
        db = self.root / 'contest.sqlite'
        with self.assertRaisesRegex(ValueError, 'phase'):
            Contest(db, phase='final')
        contest = Contest(db)
        plan_duel(contest, 'duel-retired', 'final-ladder')
        client = HttpClient(contest_handler(contest))
        path = '/contest/v1/duels'
        self.assertEqual(client.request(path + '/duel-retired/status')[0], 404)
        duels = [client.json(path, {'sides': sides}, method='POST', status=202)['duel_id']
                 for sides in (TEAMS, TEAMS[::-1])]
        self.assertEqual(len(set(duels)), 2)
        found = contest.duels.ladder_submissions(TEAMS, '1970-01-01T00:00:00+08:00')['duels']
        self.assertEqual([duel['duel_id'] for duel in found], duels[:1])
        claims = [contest.runs.claim() for _ in duels]
        self.assertEqual([request['duel_id'] for request in claims], duels)
        self.assertTrue(all(request['rules'] == 'prelim' for request in claims))
        with contest.database.transaction(write=False) as connection:
            saved = connection.execute("SELECT closed_at FROM duels WHERE duel_id = 'duel-retired'").fetchone()
            self.assertIsNone(saved['closed_at'])

    @case('L0-014', 'Portal 仅提交双方并获得新编号，校验两条查询及 Arena 查询响应')
    def test_portal_contract_and_arena_observation_validation(self):
        contest = Contest(self.root / 'http.sqlite')

        client = HttpClient(contest_handler(contest))

        path = '/contest/v1/duels'
        body = {'sides': TEAMS}
        for resource in ('status', 'result'):
            self.assertEqual(client.json(f'{path}/invalid/{resource}', status=400)['code'], 'INVALID_DUEL')
            self.assertEqual(client.json(f'{path}/duel-1/{resource}', status=404)['code'], 'DUEL_NOT_FOUND')
        self.assertEqual(client.post(path, b'{broken json', headers={'Content-Type': 'application/json'})[0], 400)
        for invalid in (dict(body, rules='prelim'), dict(body, duel_id='duel-1'),
                        {'sides': ['bot002', 'team001']}, {'sides': ['team001', 'team001']}):
            self.assertEqual(client.json(path, invalid, method='POST', status=400)['code'], 'INVALID_DUEL')
        accepted = client.json(path, body, method='POST', status=202)
        duel_id = accepted['duel_id']
        self.assertEqual(set(accepted), {'duel_id'})
        self.assertRegex(duel_id, r'^duel-[A-Za-z0-9_-]+$')
        original = contest.duels.show(duel_id)
        self.assertNotEqual(client.json(path, body, method='POST', status=202)['duel_id'], duel_id)
        self.assertEqual(contest.duels.show(duel_id), original)
        self.assertEqual(len(original['attempts']), 2)
        pending_result = {'duel_id': duel_id, 'side_results': dict.fromkeys(TEAMS, 'pending'), 'result': None}
        self.assertEqual(client.json(f'{path}/{duel_id}/result'), pending_result)
        self.assertEqual(client.json(f'{path}/{duel_id}/status'), {'duel_id': duel_id, 'state': 'in_progress'})
        removed_route = f'/contest/v1/runs/{original["attempts"][0]["run_id"]}/result'
        self.assertEqual(client.json(removed_route, {'state': 'finished'}, method='POST', status=404)['code'], 'NOT_FOUND')
        self.assertEqual(contest.duels.show(duel_id), original)
        request = contest.runs.claim()
        self.assertEqual((request['duel_id'], request['rules']), (duel_id, 'prelim'))
        self.assertIs(type(request['config']['map']['seed']), int)
        self.assertCountEqual(request['bots'], TEAMS)
        run_id = request['run_id']
        contest.runs.complete_submission(run_id, 202)
        finished = {'run_id': run_id, 'state': 'finished'}
        invalid_payloads = (
            run_query_payload(dict(finished, reason='unexpected')),
            run_query_payload({'run_id': run_id, 'state': 'platform_error', 'reason': 'unknown'}),
            run_query_payload(dict(finished, run_id='run-999.1')),
            run_query_payload(finished, missing=[run_id]),
            run_query_payload(),
            {},
        )
        for index, invalid in enumerate(invalid_payloads, 1):
            with self.subTest(invalid=invalid):
                now = index * 60
                poll = contest.runs.claim_poll(now=now)
                self.assertEqual(poll.run_id, run_id)
                contest.runs.complete_polls((poll,), 200, invalid, now=now)
                stored = contest.duels.show(duel_id)['attempts'][0]
                self.assertEqual(stored['submission'], 'accepted')
                self.assertIsNone(stored['arena_result'])
                self.assertIsNone(contest.runs.next_review())
        now = (len(invalid_payloads) + 1) * 60
        poll = contest.runs.claim_poll(now=now)
        contest.runs.complete_polls((poll,), 200, run_query_payload(finished), now=now)
        accepted = contest.duels.show(duel_id)
        contest.runs.complete_polls((poll,), 200, run_query_payload(dict(finished, state='build_failed')), now=now + 1)
        self.assertIsNone(contest.runs.claim_poll(now=now + 60))
        self.assertEqual(client.json(f'{path}/{duel_id}/result'), pending_result)
        self.assertEqual(contest.duels.show(duel_id), accepted)

    @case('L0-018', '后台白名单、账号和写标记生效且不限制本机内部接口')
    def test_admin_access_and_local_service_boundaries(self):
        roster = roster_workbook([(team, team) for team in TEAMS])
        cases = ((None, 200), (['127.0.0.0/8'], 200), ([], 403), (['192.0.2.0/24'], 403))
        with http_arena(None) as (arena_url, _, _):
            for index, (whitelist, expected) in enumerate(cases):
                with self.subTest(whitelist=whitelist):
                    directory = self.root / str(index)
                    db = directory / 'var/contest.sqlite'
                    config = write_config(
                        directory / 'contest.json', db=str(db), archive_root=str(directory / 'archives'),
                        arena_url=arena_url, host='127.0.0.1', port=0, ip_whitelist=whitelist,
                    )
                    with running_service(config) as address:
                        client = HttpClient(address)
                        # Forwarding headers must not replace the real loopback peer.
                        write = {**admin_headers(), 'X-Forwarded-For': '192.0.2.1'}
                        auth = {key: value for key, value in write.items() if key != 'X-Contest-Admin'}
                        contest = Contest(db)
                        before = contest.preliminary.progress()
                        progress = client.json('/admin/prelim/progress', headers=auth, status=expected)
                        if expected == 403:
                            self.assertEqual(progress['code'], 'IP_NOT_ALLOWED')
                            self.assertEqual(client.post('/admin/prelim/roster', roster, headers=write)[0], 403)
                            self.assertEqual(contest.preliminary.progress(), before)
                        elif whitelist is None:
                            self.assertEqual(client.request('/admin/prelim/progress')[0], 401)
                            self.assertEqual(client.post('/admin/prelim/roster', roster, headers=auth)[0], 403)
                            self.assertEqual(contest.preliminary.progress(), before)
                            imported = client.json('/admin/prelim/roster', roster, method='POST', headers=write)
                            self.assertEqual(imported['state'], 'roster_imported')

                        if whitelist == []:
                            response = client.json('/contest/v1/duels', {'sides': TEAMS}, method='POST', status=202)
                            duel_path = '/contest/v1/duels/' + response['duel_id']
                            self.assertEqual(client.json(duel_path + '/status')['state'], 'in_progress')
                            self.assertIsNone(client.json(duel_path + '/result')['result'])
