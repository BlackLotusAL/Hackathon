"""Portal and organizer HTTP contracts, including deployment and Arena calls."""
import json

from engine.protocol import dumps
from service.application import Contest
from service.config import Settings

from .helpers import (CONTEST, TEAMS, HttpClient, WorkspaceFixture, admin_headers, arena_archive,
                      case, contest_handler, http_arena, roster_workbook, run_module, running_service,
                      wait_for, write_config, write_map_pools)


class HttpServiceTests(WorkspaceFixture):
    @case('L0-014', 'Portal 提交、查询及 Arena 查询响应的校验与幂等')
    def test_portal_contract_and_arena_observation_validation(self):
        contest = Contest(self.root / 'http.sqlite')

        client = HttpClient(contest_handler(contest))

        path = '/contest/v1/duels'
        body = {'duel_id': 'duel-1', 'rules': 'prelim', 'sides': TEAMS, 'map_seed': 42}
        for resource in ('status', 'result'):
            self.assertEqual(client.json(f'{path}/invalid/{resource}', status=400)['code'], 'INVALID_DUEL')
            self.assertEqual(client.json(f'{path}/duel-1/{resource}', status=404)['code'], 'DUEL_NOT_FOUND')
        self.assertEqual(client.post(path, b'{broken json', headers={'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(client.post(path, dict(body, rules='unknown'))[0], 400)
        self.assertEqual(client.post(path, body), (202, b''))
        original = contest.duels.show('duel-1')
        self.assertEqual(client.post(path, body), (202, b''))
        self.assertEqual(client.post(path, dict(body, sides=TEAMS[::-1]))[0], 409)
        self.assertEqual(contest.duels.show('duel-1'), original)
        self.assertEqual(len(original['attempts']), 2)
        pending_result = {'duel_id': 'duel-1', 'side_results': dict.fromkeys(TEAMS, 'pending'), 'result': None}
        self.assertEqual(client.json(f'{path}/duel-1/result'), pending_result)
        self.assertEqual(client.json(f'{path}/duel-1/status'), {'duel_id': 'duel-1', 'state': 'in_progress'})
        removed_route = f'/contest/v1/runs/{original["attempts"][0]["run_id"]}/result'
        self.assertEqual(client.json(removed_route, {'state': 'finished'}, method='POST', status=404)['code'], 'NOT_FOUND')
        self.assertEqual(contest.duels.show('duel-1'), original)
        request = contest.runs.claim()
        run_id = request['run_id']
        contest.runs.complete_submission(run_id, 202)
        invalid_payloads = (
            {'run_id': run_id, 'state': 'finished', 'reason': 'unexpected'},
            {'run_id': run_id, 'state': 'platform_error', 'reason': 'unknown'},
            {'run_id': 'run-999.1', 'state': 'finished'},
            {},
        )
        for index, invalid in enumerate(invalid_payloads, 1):
            with self.subTest(invalid=invalid):
                now = index * 60
                poll = contest.runs.claim_poll(now=now)
                self.assertEqual(poll.run_id, run_id)
                contest.runs.complete_poll(poll, 200, invalid, now=now)
                stored = contest.duels.show('duel-1')['attempts'][0]
                self.assertEqual(stored['submission'], 'accepted')
                self.assertIsNone(stored['arena_result'])
                self.assertIsNone(contest.runs.next_review())
        poll = contest.runs.claim_poll(now=300)
        contest.runs.complete_poll(poll, 200, {'run_id': run_id, 'state': 'finished'}, now=300)
        accepted = contest.duels.show('duel-1')
        contest.runs.complete_poll(poll, 200, {'run_id': run_id, 'state': 'build_failed'}, now=301)
        self.assertIsNone(contest.runs.claim_poll(now=400))
        self.assertEqual(client.json(f'{path}/duel-1/result'), pending_result)
        self.assertEqual(contest.duels.show('duel-1'), accepted)

    @case('L0-018', '后台白名单、账号和写标记生效且不限制本机内部接口')
    def test_admin_access_and_local_service_boundaries(self):
        roster = roster_workbook([(team, team) for team in TEAMS])
        cases = ((None, 200), (['127.0.0.0/8'], 200), ([], 403), (['192.0.2.0/24'], 403))
        with http_arena(None) as (arena_url, _, _):
            for index, (whitelist, expected) in enumerate(cases):
                with self.subTest(whitelist=whitelist):
                    directory = self.root / str(index)
                    db = directory / 'var/contest-local.sqlite'
                    config = write_config(
                        directory / 'local.json', db=str(db), archive_root=str(directory / 'archives'),
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
                            duel = {'duel_id': 'duel-access', 'rules': 'prelim', 'sides': TEAMS, 'map_seed': 42}
                            self.assertEqual(client.post('/contest/v1/duels', duel), (202, b''))
                            duel_path = '/contest/v1/duels/duel-access'
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
                ('var/archives/contest-local', CONTEST / 'var/archives/contest-local'),
                (str(self.root / 'archives'), self.root / 'archives')):
            with self.subTest(archive=archive):
                config.write_text(dumps(dict(values, archive_root=archive)), encoding='utf-8-sig')
                settings = Settings.load(config)
                self.assertEqual(settings.db, self.root / 'data' / 'original.sqlite')
                self.assertEqual(settings.maps, directory / 'map-pools.json')
                self.assertEqual(settings.for_server().archive_root, expected)
                self.assertEqual(settings.override(db=override).for_server().archive_root, expected)

        replacement = Contest(override)
        replacement.duels.accept({'duel_id': 'duel-override', 'rules': 'prelim', 'sides': TEAMS, 'map_seed': 43})
        completed = run_module('service', '--config', config, '--db', override, 'show', 'duel-override')
        self.assertEqual(json.loads(completed.stdout)['duel_id'], 'duel-override')
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
            ('unknown-field', dumps(dict(base, unexpected=True)), 'unexpected'),
            ('retired-portal-url', dumps(dict(base, portal_url='http://127.0.0.1:8021')), 'portal_url'),
            ('retired-roster-option', dumps(dict(base, mock_roster=True)), '配置含未知字段'),
            ('missing-arena-url', dumps(required_paths), '提供 arena_url'),
            ('null-arena-url', dumps(dict(base, arena_url=None)), '提供 arena_url'),
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

    @case('L0-023', '通过 HTTP 调用 Arena 并向 Portal 提供整场结果')
    def test_http_arena_and_portal_queries_work_without_tokens(self):
        archive = self.root / 'archives'
        pools = write_map_pools(self.root / 'config/map-pools.json', 'final')
        status = {'at': '2026-11-19T15:05:00+08:00', 'slots': {'idle': 1, 'busy': 0, 'disabled': 0},
                  'queue': 0, 'done_10m': 0, 'platform_errors_1h': 0}
        with http_arena(status) as (base_url, received, arena):
            config = write_config(
                pools.with_name('production.json'), template='production', port=0, db='../var/contest.sqlite',
                host='127.0.0.1', arena_url=base_url, archive_root=str(archive), maps=pools.name,
                admin_user='file-organizer', admin_password='file-password',
            )
            with running_service(config, '--arena-url', base_url + '/arena-peer') as address:
                client = HttpClient(address)
                auth = admin_headers('file-organizer', 'file-password')
                self.assertEqual(client.json('/admin/prelim/progress', headers=auth)['state'], 'empty')
                self.assertEqual(client.request('/admin/prelim/mock-roster.xlsx', headers=auth)[0], 404)

                body = {'duel_id': 'duel-from-config', 'rules': 'final', 'sides': TEAMS}
                self.assertEqual(client.post('/contest/v1/duels', body), (202, b''))
                duel_path = '/contest/v1/duels/duel-from-config'
                self.assertEqual(client.json(duel_path + '/status')['state'], 'in_progress')
                self.assertEqual(client.json(duel_path + '/result')['side_results'], dict.fromkeys(TEAMS, 'pending'))

                submitted = received.get(timeout=5)
                self.assertEqual((submitted['method'], submitted['path']), ('POST', '/arena-peer/arena/v1/runs'))
                self.assertNotIn('authorization', submitted['headers'])
                self.assertEqual(submitted['headers']['content-type'], 'application/json')
                request = submitted['body']
                self.assertEqual((request['duel_id'], request['rules'], request['bots']),
                                 ('duel-from-config', 'final', TEAMS))
                run_id = request['run_id']
                run_path = f'/arena-peer/arena/v1/runs/{run_id}'
                lookup = received.get(timeout=5)
                self.assertEqual((lookup['method'], lookup['path']), ('GET', run_path))
                self.assertNotIn('authorization', lookup['headers'])

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
                    'duel_id': 'duel-from-config', 'side_results': dict.fromkeys(TEAMS, 'loss'), 'result': expected,
                })

                self.assertEqual(client.json('/admin/arena/status', headers=auth), status)
                queried = received.get(timeout=5)
                while queried['path'] == run_path:
                    self.assertEqual(queried['method'], 'GET')
                    self.assertNotIn('authorization', queried['headers'])
                    queried = received.get(timeout=5)
                self.assertEqual((queried['method'], queried['path']), ('GET', '/arena-peer/arena/v1/status'))
                self.assertNotIn('authorization', queried['headers'])
                self.assertTrue(received.empty())
