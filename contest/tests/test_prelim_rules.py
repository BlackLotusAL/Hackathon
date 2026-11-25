"""Preliminary board mechanics and published map constraints."""
from copy import deepcopy
from unittest import TestCase

from engine.core import Game
from engine.mapgen import generate, validate
from engine.prelim import territory_value
from engine.protocol import init_message, parse_reply, turn_message
from engine.verification import VerificationError, verify_replay
from .helpers import case


def blank_game(cells=None):
    terrain = ['.' * 41] * 41
    for (x, y), tile in (cells or {}).items():
        terrain[y] = terrain[y][:x] + tile + terrain[y][x + 1:]
    return Game({'id': 'positions', 'size': 41, 'terrain': terrain,
                 'calib': [[5, 5], [35, 35]], 'boost_zones': [
                     {'marker': [20, 20], 'cells': [[19, 20], [20, 20], [21, 20]]},
                     {'marker': [15, 15], 'cells': [[15, 15], [15, 14], [15, 13]]},
                     {'marker': [25, 25], 'cells': [[25, 25], [25, 26], [26, 25]]},
                 ]})


def place(game, seat, x, y, direction):
    beam = game.beams[seat]
    beam.x, beam.y, beam.dir = x, y, direction


def ring(game, center, radius, seat):
    x, y = center
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if max(abs(dx), abs(dy)) == radius:
                game.trail[game.idx(x + dx, y + dy)] = seat


class PreliminaryRulesTests(TestCase):
    @case('L0-038', '按起步格加速、全局可用、反向与加号指令')
    def test_boost_cells_and_commands(self):
        g = blank_game()
        place(g, 0, 18, 20, 'R')
        for command, x, speed in [('R', 19, 1), ('L', 21, 2), ('U+', 23, 2), (None, 24, 1)]:
            g.step([command, None])
            self.assertEqual((g.beams[0].x, g.beams[0].y, g.beams[0].speed), (x, 20, speed))
        for number in (1, 100, 101, 201, 600):
            for x, y, steps in [(20, 20, 2), (15, 13, 2), (25, 25, 2), (21, 21, 1)]:
                with self.subTest(turn=number, start=(x, y)):
                    g = blank_game()
                    g.turn = number - 1
                    self.assertEqual(init_message(g, 0)['map'], g.map.to_payload())
                    place(g, 0, x, y, 'R')
                    g.step([None, None])
                    self.assertEqual((g.beams[0].x, g.beams[0].speed), (x + steps, steps))
        self.assertIsNone(parse_reply('{"cmd":"U+","turn":1}'))

    @case('L0-051', '校验标记点及覆盖格，重叠加速区不叠加步数')
    def test_map_checks_explicit_boost_cells(self):
        board = generate(42)
        zone = board['boost_zones'][0]
        defect = next([x, y] for y, row in enumerate(board['terrain']) for x, tile in enumerate(row) if tile == '#')
        for cell in ([-1, 0], defect):
            invalid = deepcopy(board)
            invalid['boost_zones'][0]['cells'].append(cell)
            self.assertTrue(any('缺陷格或界外格' in problem for problem in validate(invalid)))
        missing = deepcopy(board)
        missing['boost_zones'][0]['cells'].remove(zone['marker'])
        self.assertTrue(any('标记点不在区内' in problem for problem in validate(missing)))
        overlapping = deepcopy(board)
        overlapping['boost_zones'][1]['cells'].append(zone['marker'])
        self.assertEqual(validate(overlapping), [])
        game = Game(dict(overlapping, terrain=['.' * board['size']] * board['size']))
        x, y = zone['marker']
        command, dx = ('L', -1) if x > 1 else ('R', 1)
        place(game, 0, x, y, command)
        game.step([command, None])
        self.assertEqual(game.beams[0].x, x + 2 * dx)

    @case('L0-039', '全队露边取整、板边、校准区与资源格')
    def test_perimeter(self):
        for width, height, edges, score in [(16, 1, 34, 5), (8, 2, 20, 10), (4, 4, 16, 11)]:
            g = blank_game()
            for x in range(width):
                for y in range(height):
                    g.owner[g.idx(x, y)] = 0
            self.assertEqual(territory_value(g, 0), (25, edges))
            self.assertEqual(g.score(0), 9 + score)
        g = blank_game({(7, 5): '*', (7, 4): '#'})
        g.owner[g.idx(7, 5)] = 0  # one side sealed by the home
        g.owner[g.idx(8, 5)] = 1
        self.assertEqual(territory_value(g, 0), (12, 3))
        self.assertEqual(g.score(0), 11)
        g = blank_game()
        for x in (0, 2, 4):
            g.owner[x] = 0
        self.assertEqual(g.score(0), 9 + 3 - 12 // 3)

    @case('L0-040', '加速第二步同格、扫轮廓、第一步报废与重启')
    def test_substeps_and_restart(self):
        for target_owner, alive in [(-1, (False, False)), (0, (True, False)), (1, (False, True))]:
            g = blank_game()
            place(g, 0, 10, 10, 'R')
            place(g, 1, 12, 10, 'L')
            g.owner[g.idx(11, 10)] = target_owner
            events = g.step(['R', 'L'])
            self.assertIn({'t': 'collide'}, events)
            self.assertEqual(tuple(b.alive for b in g.beams), alive)
        g = blank_game()
        place(g, 0, 10, 10, 'R')
        place(g, 1, 11, 10, 'L')
        g.owner[g.idx(10, 10)], g.owner[g.idx(11, 10)] = 1, 0
        self.assertIn({'t': 'collide'}, g.step(['R', 'L']))
        self.assertTrue(all(b.alive for b in g.beams))
        for owned in (True, False):
            g = blank_game()
            place(g, 0, 20, 20, 'R')
            place(g, 1, 23, 20, 'L')
            if owned:
                g.owner[g.idx(22, 20)] = 1
            events = g.step(['R', 'L'])
            self.assertTrue(g.beams[0].alive)
            self.assertEqual(g.beams[1].alive, owned)
            self.assertEqual((g.beams[0].x, g.beams[1].x), (22, 22))
            self.assertNotIn('collide', [event['t'] for event in events])
        g = blank_game()
        place(g, 0, 20, 20, 'R')
        g.trail[g.idx(21, 20)] = 0
        g.step(['R', 'D'])
        self.assertEqual((g.beams[0].x, g.beams[0].respawn), (21, 5))
        place(g, 1, 0, 5, 'R')
        for _ in range(4):
            g.step(['L', 'R'])
            self.assertFalse(g.beams[0].alive)
        # The enemy enters the spawn cell on the fifth downtime turn.
        g.step(['L', 'R'])
        self.assertEqual((g.beams[0].x, g.beams[0].y, g.beams[0].dir), (5, 5, 'U'))
        self.assertTrue(g.beams[0].alive)
        self.assertFalse(g.beams[1].alive)
        self.assertNotIn(1, g.trail)

    @case('L0-041', '同步闭合争格、保留新延伸的敌方轮廓、扩散穿过缺陷')
    def test_simultaneous_closures(self):
        g = blank_game()
        ring(g, (12, 15), 4, 0)
        ring(g, (12, 15), 2, 1)
        place(g, 0, 8, 15, 'L')
        place(g, 1, 10, 15, 'R')
        g.owner[g.idx(7, 15)], g.owner[g.idx(11, 15)] = 0, 1
        events = g.step(['L', 'R'])
        self.assertEqual(sum(ev['t'] == 'close' for ev in events), 2)
        for x, y in ((10, 14), (12, 15)):  # inner contour and interior are disputed
            self.assertEqual(g.owner[g.idx(x, y)], -1)
            self.assertEqual(g.trail[g.idx(x, y)], -1)
        self.assertEqual(g.owner[g.idx(9, 15)], 0)
        g = blank_game()
        ring(g, (12, 15), 3, 0)
        place(g, 0, 9, 15, 'L')
        g.owner[g.idx(8, 15)] = 0
        place(g, 1, 12, 15, 'R')
        g.step(['L', 'R'])
        self.assertEqual((g.owner[g.idx(13, 15)], g.trail[g.idx(13, 15)], g.beams[1].alive), (0, 1, True))
        g = blank_game({(12, 12): '#'})
        ring(g, (12, 15), 3, 0)
        gap = g.idx(12, 12)
        g.trail[gap] = -1
        self.assertNotIn(g.idx(12, 15), g.fill_cells(0))
        ring(g, (35, 35), 2, 0)
        g.trail[g.idx(35, 35)] = 0
        self.assertTrue(all(g.map.base[k] == -1 for k in g.fill_cells(0)))

    @case('L0-045', '只复核使用显式加速格的归档')
    def test_unsupported_replays_are_rejected(self):
        for retired in ({'stage': 'final', 'map': generate(42)},
                        {'stage': 'prelim', 'params': {'station_period': 100}, 'map': {'boost_points': []}}):
            with self.subTest(retired=retired), self.assertRaisesRegex(VerificationError, '当前赛题规则'):
                verify_replay(dict(retired, version='v1'))
