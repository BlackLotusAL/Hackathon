"""Replay storage and live-stream integration with the local practice service."""
from contextlib import redirect_stderr
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.practice.live import HumanBot, LiveMatch
from backend.practice.replay_store import ReplayStore, publish_replay
from backend.practice.server import Handler
from game_core.mapgen import generate
from game_core.protocol import dumps


def recording(map_id):
    return {'format': 'arena-replay/2', 'stage': 'prelim', 'map': {'id': map_id}, 'players': ['A', 'B'],
            'init': {}, 'frames': [], 'result': {'winner': 0, 'score': [10, 7]}}


class LiveResponse(BytesIO):
    def __init__(self, on_first_flush=None):
        super().__init__()
        self.on_first_flush = on_first_flush

    def flush(self):
        callback, self.on_first_flush = self.on_first_flush, None
        if callback:
            callback()

    def events(self):
        return [json.loads(line[6:]) for line in self.getvalue().splitlines() if line.startswith(b'data: ')]


def stream(live, on_first_flush=None):
    wire = LiveResponse(on_first_flush)
    handler = SimpleNamespace(wfile=wire, send_response=lambda status: None,
                              send_header=lambda name, value: None, end_headers=lambda: None)
    Handler.stream(handler, live)
    return wire.events()


class PracticeTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix='practice-test-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.kit = self.root / 'kit'
        self.store = ReplayStore(self.root / 'practice', kit=self.kit)

    def write_recording(self, path, map_id):
        path.parent.mkdir(parents=True, exist_ok=True)
        publish_replay(path, recording(map_id))

    def test_listing_and_loading_use_only_the_selected_recording(self):
        local = self.store.root / 'same-run'
        self.write_recording(local / 'replay.json', 'current')
        (local / 'result.json').write_text('{"map":"stale-summary"}', encoding='utf-8')
        self.write_recording(self.kit / 'runs/server/same-run/replay.json', 'other-root')
        self.write_recording(self.kit / 'runs/unlisted/replay.json', 'other-root')
        self.write_recording(self.kit / 'replays/demo.json', 'sample')
        imported = self.store.import_replay(dumps(recording('imported')).encode('utf-8'))

        listing = self.store.list_replays()
        self.assertEqual({row['id']: (row['source'], row['map']) for row in listing}, {
            'same-run': ('local', 'current'), 'smp-demo': ('sample', 'sample'), imported: ('import', 'imported'),
        })
        for row in listing:
            replay = self.store.load_replay(row['id'])
            self.assertEqual((row['map'], row['score']), (replay['map']['id'], replay['result']['score']))
        self.assertEqual(list((self.store.root / 'imports').glob('*.meta')), [])
        with self.assertRaises(KeyError):
            self.store.load_replay('unlisted')

        self.write_recording(local / 'replay.json', 'updated')
        refreshed = {row['id']: row['map'] for row in self.store.list_replays()}
        self.assertEqual(refreshed['same-run'], 'updated')
        self.assertEqual(self.store.load_replay('same-run')['map']['id'], 'updated')

    def make_live(self):
        players = [(HumanBot(), 'A', None), (HumanBot(), 'B', None)]
        return LiveMatch({'paused': True}, generate(42), players, self.store.root)

    def test_stream_sends_snapshot_then_new_events_and_reconnects_to_full_history(self):
        live = self.make_live()

        def finish_after_snapshot():
            live.control('stop')
            live.run()

        first, *updates = stream(live, finish_after_snapshot)
        self.assertEqual((first['ev'], first['events'], first['status']['paused']), ('snapshot', [], True))
        self.assertEqual([event['ev'] for event in updates], ['status', 'header', 'end'])
        reconnected, = stream(live)
        self.assertEqual(reconnected['events'], updates)
        self.assertEqual(reconnected['status']['status'], 'done')
        self.assertEqual(self.store.load_replay(live.id)['result'], {
            key: reconnected['status']['result'][key] for key in ('winner', 'reason', 'turns', 'score', 'bonus',
                                                                 'lead_turns', 'time_us')
        })

    def test_startup_error_can_finish_without_inventing_a_header(self):
        live = self.make_live()
        with patch.object(live.match, 'run', side_effect=RuntimeError('无法启动 Bot')), redirect_stderr(StringIO()):
            live.run()
        response, = stream(live)
        event, = response['events']
        self.assertEqual((event['ev'], event['result'], event['error']),
                         ('end', None, 'RuntimeError: 无法启动 Bot'))
        self.assertEqual(self.store.list_replays(), [])


if __name__ == '__main__':
    unittest.main()
