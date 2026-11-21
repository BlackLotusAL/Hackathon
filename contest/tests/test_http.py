"""Portal and organizer HTTP contracts, including deployment and Arena calls."""
import json

from engine.protocol import dumps
from service.config import Settings
from service.server import Contest

from .helpers import (CONTEST, TEAMS, HttpClient, WorkspaceFixture, admin_headers, arena_archive,
                      case, contest_handler, http_arena, roster_workbook, run_module, run_query_payload,
                      running_service, sealed_maps, wait_for, write_config)


class HttpServiceTests(WorkspaceFixture):
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

    @case('L0-021', '配置路径独立于工作目录且命令行可覆盖数据库')
    def test_file_paths_and_cli_overrides_are_deterministic(self):
        directory = self.root / 'configuration'
        directory.mkdir()
        config = directory / 'contest.json'
        values = {'db': '../data/original.sqlite', 'maps': 'map-pools.json',
                  'arena_url': 'http://127.0.0.1:18080'}
        override = self.root / 'override.sqlite'
        for archive, expected in (
                ('var/archives/contest', CONTEST / 'var/archives/contest'),
                (str(self.root / 'archives'), self.root / 'archives')):
            with self.subTest(archive=archive):
                config.write_text(dumps(dict(values, archive_root=archive)), encoding='utf-8-sig')
                settings = Settings.load(config)
                self.assertEqual(settings.db, self.root / 'data' / 'original.sqlite')
                self.assertEqual(settings.maps, directory / 'map-pools.json')
                self.assertEqual(settings.for_server().archive_root, expected)
                self.assertEqual(settings.override(db=override).for_server().archive_root, expected)

        replacement = Contest(override)
        duel_id = replacement.duels.accept({'sides': TEAMS})['duel_id']
        completed = run_module('service', '--config', config, '--db', override, 'show', duel_id)
        self.assertEqual(json.loads(completed.stdout)['duel_id'], duel_id)
        self.assertFalse((self.root / 'data').exists())
        self.assertFalse((self.root / 'archives').exists())

    @case('L0-022', '错误配置在创建目录、数据库和监听服务之前被拒绝')
    def test_invalid_configuration_creates_no_persistent_state(self):
        config = self.root / 'contest.json'
        required_paths = {'db': 'var/contest.sqlite', 'archive_root': str(self.root / 'var/archives')}
        base = dict(required_paths, arena_url='http://127.0.0.1:18080')
        cases = (
            ('broken-json', '{broken json', None),
            ('invalid-port', dumps(dict(base, port=65536)), 'port'),
            ('invalid-phase', dumps(dict(base, phase='unknown')), 'phase'),
            ('unknown-field', dumps(dict(base, unexpected=True)), 'unexpected'),
            ('missing-arena-url', dumps(required_paths), '提供 arena_url'),
            ('invalid-arena-url', dumps(dict(base, arena_url='')), 'HTTP 基础 URL'),
            ('missing-shared-archive', dumps(dict(base, archive_root=None)), '提供 archive_root'),
        )
        for scenario, content, message in cases:
            with self.subTest(scenario=scenario):
                config.write_text(content, encoding='utf-8')
                completed = run_module('service', '--config', config, 'serve', expected=1)
                self.assertNotIn('Traceback', completed.stderr)
                if message is not None:
                    self.assertIn(message, completed.stderr)
                self.assertFalse((self.root / 'var').exists())

    @case('L0-023', '决赛经统一接口查询单个执行并向 Portal 提供整场结果')
    def test_http_arena_and_portal_queries_work_without_tokens(self):
        archive = self.root / 'archives'
        status = {'at': '2026-11-19T15:05:00+08:00', 'slots': {'idle': 1, 'busy': 0, 'disabled': 0},
                  'queue': 0, 'done_10m': 0, 'platform_errors_1h': 0}
        with http_arena(status) as (base_url, received, arena):
            config = write_config(
                self.root / 'config/contest.json', port=0, db='../var/contest.sqlite',
                host='127.0.0.1', arena_url=base_url, archive_root=str(archive), phase='final',
                admin_user='file-organizer', admin_password='file-password',
            )
            with running_service(config, '--arena-url', base_url + '/arena-peer') as address:
                client = HttpClient(address)
                auth = admin_headers('file-organizer', 'file-password')
                self.assertEqual(client.json('/admin/prelim/progress', headers=auth)['state'], 'empty')

                body = {'sides': TEAMS}
                accepted = client.json('/contest/v1/duels', body, method='POST', status=202)
                duel_id = accepted['duel_id']
                duel_path = '/contest/v1/duels/' + duel_id
                self.assertEqual(client.json(duel_path + '/status')['state'], 'in_progress')
                self.assertEqual(client.json(duel_path + '/result')['side_results'], dict.fromkeys(TEAMS, 'pending'))

                submitted = received.get(timeout=5)
                self.assertEqual((submitted['method'], submitted['path']), ('POST', '/arena-peer/arena/v1/runs'))
                self.assertNotIn('authorization', submitted['headers'])
                self.assertEqual(submitted['headers']['content-type'], 'application/json')
                request = submitted['body']
                self.assertEqual((request['duel_id'], request['rules']), (duel_id, 'final'))
                self.assertCountEqual(request['bots'], TEAMS)
                pool_config = CONTEST / 'config/map-pools.example.json'
                entry = json.loads(pool_config.read_bytes())['final'][0]
                self.assertEqual(request['config']['map'], {
                    'file': (pool_config.parent / entry['file']).read_bytes().decode('utf-8'),
                    'sha256': entry['sha256'],
                })
                run_id = request['run_id']
                query_path = '/arena-peer/arena/v1/runs/query'

                def check_run_query(event):
                    self.assertEqual((event['method'], event['path']), ('POST', query_path))
                    self.assertEqual(event['body'], {'run_ids': [run_id]})
                    self.assertEqual(event['headers']['content-type'], 'application/json')
                    self.assertNotIn('authorization', event['headers'])

                check_run_query(received.get(timeout=5))

                bots = [{'team_id': team, 'commit': f'{index:040x}', 'state': 'failed'}
                        for index, team in enumerate(request['bots'], 1)]
                arena_archive(archive / 'runs' / run_id, bots, state='build_failed')
                arena.set_state(run_id, 'build_failed')
                expected = {
                    'outcome': 'double_loss', 'winner': None, 'games_won': dict.fromkeys(TEAMS, 0),
                    'games_lost': dict.fromkeys(TEAMS, 6), 'runs': [], 'games': [], 'build_run_id': run_id,
                }
                wait_for(lambda: client.json(duel_path + '/status'), lambda duel: duel['state'] == 'finished')
                self.assertEqual(client.json(duel_path + '/result'), {
                    'duel_id': duel_id, 'side_results': dict.fromkeys(TEAMS, 'loss'), 'result': expected,
                })

                self.assertEqual(client.json('/admin/arena/status', headers=auth), status)
                queried = received.get(timeout=5)
                while queried['path'] == query_path:
                    check_run_query(queried)
                    queried = received.get(timeout=5)
                self.assertEqual((queried['method'], queried['path']), ('GET', '/arena-peer/arena/v1/status'))
                self.assertNotIn('authorization', queried['headers'])
                self.assertTrue(received.empty())
