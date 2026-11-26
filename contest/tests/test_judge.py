"""Exercise judge tasks and the Contest archive review contract."""
import json
import shutil
from unittest.mock import patch

from engine import VERSION
from engine.match import HttpBot
from engine.protocol import dumps, json_object
from engine.replay import game_result, verify_replay
from judge.task import ContainerBot, run_task
from service.arena import review_archive
from service.server import Contest, ContestWorker

from .helpers import (ArenaStub, Bot, MatchFixture, arena_archive, case, http_bots, plan_duel, run_module,
                      sealed_maps, write_judge_task)


class JudgeTests(MatchFixture):
    @case('L0-049', '回合连接中断后在时限内收到容器退出标记')
    def test_exit_marker_after_connection_break(self):
        marker = self.root / 'exited.json'
        bot = ContainerBot('http://localhost:8080', marker)
        clock = [0.0]

        def publish_after_delay(seconds):
            clock[0] += seconds
            if clock[0] >= 0.09:
                marker.write_text('{"exit_code":1,"oom":false}', encoding='utf-8')

        transport = ('connection', 0, 'closed', 2000)
        now = patch('judge.task.time.monotonic', side_effect=lambda: clock[0])
        delay = patch('judge.task.time.sleep', side_effect=publish_after_delay)
        with patch.object(HttpBot, 'request', return_value=transport), now, delay:
            reply = bot.request('POST', '/v1/matches/run-1.1/turns/3', {'type': 'turn'}, 0.2)
        self.assertEqual(reply, transport)
        self.assertTrue(bot.exited())
        self.assertLessEqual(clock[0], 0.2)

    @case('L0-001', '双方 Bot 服务共用完整执行编号，归档可通过同版复核')
    def test_judge_entrypoint_produces_verifiable_archives(self):
        stage, run_id = 'prelim', 'run-1.1'
        with http_bots(run_id) as specs:
            request = self.request(stage, run_id)
            request['config']['map'] = sealed_maps(stage)[0]
            task = write_judge_task(self.root / ('task-' + stage), dict(request, bots=specs))
            archive_root = self.root / 'entrypoint'
            output = archive_root / 'runs' / run_id
            completed = run_module('judge', '--task', task, '--out', output)
            result = json_object((output / 'result.json').read_bytes())
            self.assertEqual(json.loads(completed.stdout), result)
            self.assertEqual((result['run_id'], result['version'], result['stage']), (run_id, VERSION, stage))
            self.assertEqual((result['winner'], result['reason'], result['turns'], result['errors']),
                             (0, 'limit', 600, [0, 0]))
            self.assertGreater(result['score'][0], result['score'][1])
            replay = json_object((output / 'replay.json').read_bytes())
            self.assertEqual(replay['version'], result['version'])
            for document in (result, replay):
                self.assertFalse({'format', 'engine_version'} & document.keys())
            self.assertEqual(len(replay['frames']), 600)
            self.assertEqual(len(replay['observations']), 602)
            self.assertNotIn('station_period', replay['params'])
            self.assertEqual(replay['map']['boost_zones'], json.loads(request['config']['map']['file'])['boost_zones'])
            self.assertEqual(set(replay['result']), {'turns', 'winner', 'reason', 'score', 'lead_turns', 'time_us'})
            self.assertEqual(set(replay['init']), {'owner', 'contour', 'beams', 'score',
                                                  'lead_turns', 'time_us'})
            sealed = request['config']['map']
            map_file = self.root / f'{stage}.json'
            map_file.write_bytes(sealed['file'].encode('utf-8'))
            verified = run_module('engine', 'verify-replay', output / 'replay.json',
                                  '--log-file', output / 'log.jsonl',
                                  '--map-file', map_file, '--map-sha256', sealed['sha256'])
            self.assertEqual(json.loads(verified.stdout), replay['result'])
            # Extra fields in an earlier archive with the same gameplay may be ignored.
            compatible = json_object((output / 'replay.json').read_bytes())
            compatible['source'] = 'practice'
            compatible['map']['old_metadata'] = []
            for snapshot in [compatible['init'], *compatible['frames']]:
                for beam in snapshot['beams']:
                    beam['old_metadata'] = 0
            board = json_object(sealed['file'].encode('utf-8'))
            self.assertEqual(game_result(verify_replay(compatible, board=board)), replay['result'])
            expected = arena_archive(output, self.metadata(request))
            manifest = review_archive(archive_root, request, 'finished', ladder=False)
            self.assertEqual(manifest, expected)

    @case('L0-050', '裁判不会按旧赛段规则启动新任务')
    def test_judge_rejects_retired_stage_even_with_override(self):
        task = write_judge_task(self.root / 'task', self.request('final'))
        output = self.root / 'out'
        with self.assertRaisesRegex(ValueError, '当前赛题规则'):
            run_task(task, output, 'prelim')
        self.assertFalse(output.exists())

    @case('L0-015', '正式赛和天梯归档缺失、篡改或版本不符时暂缓结算')
    def test_invalid_archives_remain_held_without_settlement(self):
        for kind in ('prelim', 'prelim-ladder'):
            stage = kind.split('-')[0]
            ladder = kind.endswith('-ladder')
            request = self.request(stage)
            pools = {}
            if ladder:
                request['bots'] = [request['bots'][0], 'bot002']
            else:
                pools[stage] = sealed_maps(stage)
                request['config']['map'] = pools[stage][0]
            corpus = self.root / ('corpus-' + kind)
            corpus.mkdir()
            started_at = '2026-11-01T14:05:00+08:00' if ladder else None
            output, manifest, replay = self.judge_archive(
                request, [Bot('U', us=1001), Bot(None, status='invalid', us=2001)],
                archive_root=corpus, started_at=started_at)
            self.assertEqual(review_archive(corpus, request, 'finished', ladder=ladder), manifest)
            changes = self.archive_changes(output, manifest, replay, ladder=ladder)
            for name, filename, content in changes:
                with self.subTest(kind=kind, corruption=name):
                    root = self.root / f'{kind}-{name}'
                    root.mkdir()
                    archive = root / 'archives'
                    target = archive / 'runs' / request['run_id']
                    shutil.copytree(output, target)
                    evidence = target / filename
                    if content is None:
                        evidence.unlink()
                    else:
                        evidence.write_text(content, encoding='utf-8')
                    contest = Contest(root / 'contest.sqlite', pools)
                    plan_duel(contest, 'duel-1', kind, request['bots'])
                    arena = ArenaStub()
                    worker = ContestWorker(contest, archive, arena)
                    self.assertTrue(worker.submit_next())
                    self.assertEqual(arena.submitted, [request])
                    arena.set_state(request['run_id'], 'finished')
                    if kind == 'prelim':
                        worker.poll_preliminary_round()
                    else:
                        self.assertTrue(worker.poll_next())
                    self.assertTrue(worker.review_next())
                    stored = contest.duels.show('duel-1')
                    self.assertEqual(stored['attempts'][0]['review'], 'held')
                    self.assertTrue(stored['attempts'][0]['issue'])
                    self.assertEqual(contest.rulings.pending(), {'pending': []})
                    self.assertIsNone(stored['result'])
                    self.assertFalse(worker.poll_next())

    @staticmethod
    def archive_changes(output, manifest, replay, *, ladder):
        single = manifest['result']
        without_run_id = {key: value for key, value in single.items() if key != 'run_id'}
        yield ('missing-run-id', 'match.json', dumps(dict(manifest, result=without_run_id)))
        yield ('wrong-manifest-version', 'match.json', dumps(dict(manifest, result=dict(single, version='v2'))))
        yield ('wrong-replay-version', 'replay.json', dumps(dict(replay, version='v2')))
        yield ('missing-public-observations', 'replay.json', dumps(
            {key: value for key, value in replay.items() if key != 'observations'}))
        for seat in (0, 1):
            bots = [dict(bot) for bot in manifest['bots']]
            bots[seat]['commit'] = ' '
            yield (f'invalid-commit-at-seat-{seat}', 'match.json', dumps(dict(manifest, bots=bots)))
        if ladder:
            return

        log = [json_object(line) for line in (output / 'log.jsonl').read_bytes().splitlines()]
        timing = [log[1]['us'][0] + 1, log[1]['us'][1]]
        score = [log[1]['score'][0] + 1, log[1]['score'][1]]
        changed_owner = '0' if replay['init']['owner'][0] == '.' else '.'
        changed_init = dict(replay['init'], owner=changed_owner + replay['init']['owner'][1:])
        changed_frames = [dict(replay['frames'][0], score=score), *replay['frames'][1:]]
        yield from (
            ('missing-manifest', 'match.json', None),
            ('missing-replay', 'replay.json', None),
            ('missing-log', 'log.jsonl', None),
            ('wrong-score', 'match.json', dumps(dict(manifest, result=dict(
                single, score=[single['score'][0] + 1, single['score'][1]])))),
            ('wrong-map-hash', 'match.json', dumps(dict(manifest, result=dict(single, map_sha256='0' * 64)))),
            ('swapped-teams', 'match.json', dumps(dict(manifest, bots=manifest['bots'][::-1]))),
            ('edited-initial-grid', 'replay.json', dumps(dict(replay, init=changed_init))),
            ('edited-frame-score', 'replay.json', dumps(dict(replay, frames=changed_frames))),
            ('edited-log-timing', 'log.jsonl', '\n'.join(map(dumps, [log[0], dict(log[1], us=timing), *log[2:]])) + '\n'),
            ('edited-log-score', 'log.jsonl', '\n'.join(map(dumps, [log[0], dict(log[1], score=score), *log[2:]])) + '\n'),
        )
