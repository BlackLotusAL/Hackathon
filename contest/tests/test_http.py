"""Exercise Portal, Arena and organizer HTTP contracts."""
from service.server import Contest

from .helpers import (TEAMS, Bot, HttpClient, MatchFixture, admin_headers, arena_archive,
                      case, contest_handler, http_arena, roster_workbook, run_query_payload,
                      running_service, sealed_maps, wait_for, write_config, write_map_pools)


class HttpServiceTests(MatchFixture):
    @case('L0-035', '手动对决可同队模拟，按所选规则执行，并从后台读取归档回放')
    def test_manual_duels_reach_arena_with_selected_rules(self):
        archive = self.root / 'archives'
        database = self.root / 'contest.sqlite'
        pools = write_map_pools(self.root / 'config/maps.json', 'final')
        path = '/admin/duels'
        with http_arena(None) as (arena_url, received, arena):
            config = write_config(
                pools.with_name('contest.json'), maps=pools.name, port=0, db=str(database),
                host='127.0.0.1', arena_url=arena_url, archive_root=str(archive), phase='prelim',
            )
            with running_service(config) as address:
                client = HttpClient(address)
                admin = HttpClient(address, headers=admin_headers())
                prelim = {'sides': TEAMS[:1] * 2, 'rules': 'prelim'}
                final = {'sides': TEAMS, 'rules': 'final'}
                self.assertEqual(client.post(path, prelim)[0], 401)
                self.assertEqual(admin.post(path, dict(final, sides=[TEAMS[0], 'bot001']))[0], 400)
                self.assertEqual(admin.request('/admin/replay.js')[0], 200)

                def submitted():
                    event = received.get(timeout=5)
                    while event['path'] != '/arena/v1/runs':
                        event = received.get(timeout=5)
                    return event['body']

                duel_ids = []
                for body, games in ((prelim, 2), (final, 6)):
                    accepted = admin.json(path, body, method='POST', status=202)
                    duel_id = accepted['duel_id']
                    duel_ids.append(duel_id)
                    detail_path = f'{path}/{duel_id}'
                    detail = admin.json(detail_path)
                    self.assertEqual((detail['rules'], detail['sides'], len(detail['attempts'])),
                                     (body['rules'], body['sides'], games))
                    self.assertEqual(client.request(f'/contest/v1/duels/{duel_id}/result')[0], 404)
                    if body == prelim:
                        self.assertEqual(admin.json(path, final, method='POST', status=409)['code'], 'PHASE_BUSY')

                    request = submitted()
                    self.assertEqual((request['duel_id'], request['rules']), (duel_id, body['rules']))
                    self.assertCountEqual(request['bots'], body['sides'])
                    map_fields = {'seed'} if body == prelim else {'file', 'sha256'}
                    self.assertEqual(set(request['config']['map']), map_fields)

                    run_id = request['run_id']
                    replay_path = f'/admin/runs/{run_id}/replay'
                    self.assertEqual(admin.request(replay_path)[0], 404)
                    if body == prelim:
                        started = '2026-11-19T15:05:00+08:00'
                        _, _, drawn = self.judge_archive(request, started_at=started, archive_root=archive)
                        self.assertIsNone(drawn['result']['winner'])
                        arena.set_state(run_id, 'finished')
                        second = submitted()
                        self.assertEqual(second, dict(request, run_id=second['run_id']))
                        self.assertEqual(client.request(replay_path)[0], 401)
                        self.assertEqual(admin.json(replay_path), drawn)
                        _, _, winning = self.judge_archive(second, [Bot('U'), Bot('D', exit_after_reply=True)],
                                                           started_at=started, archive_root=archive)
                        arena.set_state(second['run_id'], 'finished')
                        settled = wait_for(lambda: admin.json(detail_path), lambda duel: duel['state'] == 'finished')
                        self.assertEqual(settled['result']['runs'], [run_id, second['run_id']])
                        self.assertEqual(settled['result']['winner'], 0)
                        self.assertEqual(settled['result']['games_won'], {'0': 1, '1': 0})
                        self.assertTrue(all(run['has_replay'] for run in settled['attempts']))
                        self.assertEqual(admin.json(f"/admin/runs/{second['run_id']}/replay"), winning)
                        self.assertEqual(admin.json('/admin/rulings')['pending'], [])
                        continue

                    bots = [{'team_id': team, 'commit': f'{index:040x}', 'state': 'failed'}
                            for index, team in enumerate(request['bots'], 1)]
                    arena_archive(archive / 'runs' / run_id, bots, state='build_failed')
                    arena.set_state(run_id, 'build_failed')
                    settled = wait_for(lambda: admin.json(detail_path), lambda duel: duel['state'] == 'finished')
                    self.assertEqual(settled['result']['games_lost'], dict.fromkeys(TEAMS, games))
                    self.assertEqual(settled['result']['build_run_id'], run_id)
                    self.assertEqual(admin.request(replay_path)[0], 404)
                    self.assertFalse(settled['attempts'][0]['has_replay'])

                self.assertEqual([duel['duel_id'] for duel in admin.json(path)['duels']], duel_ids[::-1])
                stopped_id = admin.json(path, dict(final, sides=prelim['sides']), method='POST', status=202)['duel_id']
                admin.json(f'{path}/{stopped_id}/stop', method='POST')
                self.assertEqual(admin.json(f'{path}/{stopped_id}')['state'], 'stopped')
                self.assertEqual(admin.json('/admin/prelim/progress')['state'], 'empty')
                found = Contest(database).duels.ladder_submissions(TEAMS, '1970-01-01T00:00:00+08:00')
                self.assertEqual(found['duels'], [])

    @case('L0-030', '正式决赛需要固定地图池，只受理队伍之间的对决')
    def test_final_duels_require_a_fixed_pool_and_two_teams(self):
        db = self.root / 'final.sqlite'
        path = '/contest/v1/duels'
        client = HttpClient(contest_handler(Contest(db, phase='final')))
        self.assertEqual(client.json(path, {'sides': TEAMS}, method='POST', status=503)['code'],
                         'MAP_POOL_UNAVAILABLE')

        maps = sealed_maps('final')
        contest = Contest(db, {'final': maps}, phase='final')
        client = HttpClient(contest_handler(contest))
        self.assertEqual(client.json(path, {'sides': [TEAMS[0], 'bot002']}, method='POST', status=400)['code'],
                         'INVALID_DUEL')
        duels = [client.json(path, {'sides': sides}, method='POST', status=202)['duel_id']
                 for sides in (TEAMS, TEAMS[::-1])]
        self.assertEqual(len(set(duels)), 2)

        found = contest.duels.ladder_submissions(TEAMS, '1970-01-01T00:00:00+08:00')['duels']
        self.assertEqual([duel['duel_id'] for duel in found], duels[:1])
        self.assertEqual([contest.runs.claim()['duel_id'] for _ in duels], duels)

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
