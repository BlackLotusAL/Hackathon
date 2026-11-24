"""Exercise judge tasks and the Contest archive review contract."""
import json
import shutil

from engine import VERSION
from engine.protocol import dumps, json_object
from service.arena import review_archive
from service.server import Contest, ContestWorker

from .helpers import (ArenaStub, Bot, MatchFixture, arena_archive, case, http_bots, plan_duel, run_module,
                      sealed_maps, write_judge_task)


class JudgeTests(MatchFixture):
    @case('L0-001', '初决赛双方 Bot 服务共用完整执行编号，归档可通过同版复核')
    def test_judge_entrypoint_produces_verifiable_archives(self):
        for stage, run_id in (('prelim', 'run-1.1'), ('final', 'run-2.2')):
            with self.subTest(stage=stage), http_bots(run_id) as specs:
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
                self.assertEqual('latent' in replay['init'], stage == 'final')
                self.assertEqual('latent' in replay['params'], stage == 'final')
                sealed = request['config']['map']
                map_file = self.root / f'{stage}.json'
                map_file.write_bytes(sealed['file'].encode('utf-8'))
                verified = run_module('engine', 'verify-replay', output / 'replay.json',
                                      '--log-file', output / 'log.jsonl',
                                      '--map-file', map_file, '--map-sha256', sealed['sha256'])
                self.assertEqual(json.loads(verified.stdout), replay['result'])
                expected = arena_archive(output, self.metadata(request))
                manifest = review_archive(archive_root, request, 'finished', ladder=False)
                self.assertEqual(manifest, expected)

    @case('L0-015', '正式赛和天梯归档缺失、篡改或版本不符时暂缓结算')
    def test_invalid_archives_remain_held_without_settlement(self):
        for kind in ('prelim', 'final', 'prelim-ladder'):
            stage = kind.split('-')[0]
            ladder = kind.endswith('-ladder')
            request = self.request(stage)
            pools = {}
            if not ladder:
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
        yield ('practice-replay', 'replay.json', dumps(dict(replay, source='practice')))
        yield ('missing-public-observations', 'replay.json', dumps(
            {key: value for key, value in replay.items() if key != 'observations'}))
        if ladder:
            return

        log = [json_object(line) for line in (output / 'log.jsonl').read_bytes().splitlines()]
        timing = [log[1]['us'][0] + 1, log[1]['us'][1]]
        score = [log[1]['score'][0] + 1, log[1]['score'][1]]
        changed_owner = '0' if replay['init']['owner'][0] == '.' else '.'
        changed_init = dict(replay['init'], owner=changed_owner + replay['init']['owner'][1:])
        changed_frames = [dict(replay['frames'][0], score=score), *replay['frames'][1:]]
        missing_commit = [dict(bot) for bot in manifest['bots']]
        missing_commit[0].pop('commit')
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
            ('missing-commit', 'match.json', dumps(dict(manifest, bots=missing_commit))),
        )
