"""Exercise judge tasks and the Contest archive review contract."""
import json
import shutil

from engine.core import ENGINE_VERSION
from engine.protocol import dumps, json_object
from service.application import Contest, ContestWorker
from service.executions import review_archive

from .helpers import (ArenaStub, Bot, MatchFixture, arena_archive, case, http_bots, run_module,
                      sealed_maps, write_judge_task)


class JudgeTests(MatchFixture):
    @case('L0-001', '初赛和决赛裁判无令牌运行完整对局并通过同版复核')
    def test_judge_entrypoint_produces_verifiable_archives(self):
        with http_bots() as specs:
            for number, stage in enumerate(('prelim', 'final'), 1):
                with self.subTest(stage=stage):
                    request = self.request(stage, f'run-{number}.1')
                    request['config']['map'] = sealed_maps(stage)[0]
                    task = write_judge_task(self.root / ('task-' + stage), dict(request, bots=specs))
                    archive_root = self.root / 'entrypoint'
                    output = archive_root / 'runs' / request['run_id']
                    completed = run_module('judge', '--task', task, '--out', output,
                                           env={'ARENA_STAGE': 'ignored-env'})
                    result = json_object((output / 'result.json').read_bytes())
                    self.assertEqual(json.loads(completed.stdout), result)
                    self.assertEqual((result['match_id'], result['engine_version'], result['stage']),
                                     (request['run_id'], ENGINE_VERSION, stage))
                    self.assertEqual((result['winner'], result['reason'], result['turns'], result['errors']),
                                     (0, 'limit', 600, [0, 0]))
                    self.assertGreater(result['score'][0], result['score'][1])
                    replay = json_object((output / 'replay.json').read_bytes())
                    self.assertEqual(len(replay['frames']), 600)
                    self.assertEqual(len(replay['observations']), 602)
                    self.assertEqual('latent' in replay['init'], stage == 'final')
                    verified = run_module('engine', 'verify-replay', output / 'replay.json')
                    self.assertEqual(json.loads(verified.stdout), replay['result'])
                    expected = arena_archive(output, self.metadata(request))
                    manifest, issue = review_archive(archive_root, request, 'finished', ladder=False)
                    self.assertEqual(manifest, expected)
                    self.assertIsNone(issue)

    @case('L0-024', '裁判无令牌任务按 rules 执行且支持显式赛段覆盖')
    def test_judge_uses_task_rules_and_explicit_stage_override(self):
        catalog = self.root / 'unused-map-catalog.json'
        catalog.write_text('invalid JSON', encoding='utf-8')
        environment = {'ARENA_STAGE': 'invalid-env', 'ARENA_MAPS': str(catalog)}
        for number, (rules, override, expected) in enumerate((
                ('final', None, 'final'), (None, None, 'prelim'), ('prelim', 'final', 'final')), 1):
            with self.subTest(rules=rules, override=override):
                root = self.root / str(number)
                task = {'run_id': f'run-{number}.1', 'config': {'map': {'seed': 42}},
                        'bots': [{'url': f'http://bot{seat}:8080'} for seat in (0, 1)]}
                if rules is not None:
                    task['rules'] = rules
                path = write_judge_task(root, task, exited=True)
                arguments = ('--stage', override) if override else ()
                completed = run_module('judge', '--task', path, '--out', root / 'out', *arguments,
                                       env=environment)
                result = json.loads(completed.stdout)
                self.assertEqual((result['stage'], result['map']), (expected, 'R42'))

    @case('L0-015', '初决赛归档缺失、篡改和版本异常均暂缓结算')
    def test_invalid_archives_remain_held_without_settlement(self):
        for stage in ('prelim', 'final'):
            maps = sealed_maps(stage)
            request = self.request(stage)
            request['config']['map'] = maps[0]
            corpus = self.root / ('corpus-' + stage)
            corpus.mkdir()
            output, manifest, replay = self.judge_archive(
                request, [Bot('U', us=1001), Bot('D', us=2001)], archive_root=corpus)
            self.assertEqual(review_archive(corpus, request, 'finished', ladder=False), (manifest, None))
            single = manifest['result']
            log = [json_object(line) for line in (output / 'log.jsonl').read_bytes().splitlines()]
            modified_log = [dict(entry) for entry in log]
            modified_log[1]['us'] = [log[1]['us'][0] + 1, log[1]['us'][1]]
            missing_commit = [dict(bot) for bot in manifest['bots']]
            missing_commit[0].pop('commit')
            changes = (
                ('missing-manifest', 'match.json', None),
                ('missing-replay', 'replay.json', None),
                ('missing-log', 'log.jsonl', None),
                ('wrong-score', 'match.json', dumps(dict(manifest, result=dict(
                    single, score=[single['score'][0] + 1, single['score'][1]])))),
                ('wrong-map-hash', 'match.json', dumps(dict(manifest, result=dict(single, map_sha256='0' * 64)))),
                ('swapped-teams', 'match.json', dumps(dict(manifest, bots=manifest['bots'][::-1]))),
                ('unknown-engine', 'match.json', dumps(dict(manifest, result=dict(single, engine_version='unknown/version')))),
                ('missing-public-observations', 'replay.json', dumps(
                    {key: value for key, value in replay.items() if key != 'observations'})),
                ('edited-log-timing', 'log.jsonl', '\n'.join(map(dumps, modified_log)) + '\n'),
                ('missing-commit', 'match.json', dumps(dict(manifest, bots=missing_commit))),
            )
            for name, filename, content in changes:
                with self.subTest(stage=stage, corruption=name):
                    root = self.root / f'{stage}-{name}'
                    root.mkdir()
                    archive = root / 'var/archives/contest'
                    target = archive / 'runs' / request['run_id']
                    shutil.copytree(output, target)
                    evidence = target / filename
                    if content is None:
                        evidence.unlink()
                    else:
                        evidence.write_text(content, encoding='utf-8')
                    contest = Contest(root / 'contest.sqlite', {stage: maps})
                    contest.duels.accept({'duel_id': 'duel-1', 'rules': stage, 'sides': request['bots']})
                    arena = ArenaStub()
                    worker = ContestWorker(contest, archive, arena)
                    self.assertTrue(worker.submit_next())
                    self.assertEqual(arena.submitted, [request])
                    arena.set_state(request['run_id'], 'finished')
                    if stage == 'prelim':
                        worker.poll_preliminary_round()
                    else:
                        self.assertTrue(worker.poll_next())
                    self.assertTrue(worker.review_next())
                    stored = contest.duels.show('duel-1')
                    self.assertEqual(stored['attempts'][0]['review'], 'held')
                    self.assertTrue(stored['attempts'][0]['issue'])
                    self.assertIsNone(stored['result'])
                    self.assertFalse(worker.submit_next())
                    self.assertFalse(worker.poll_next())
