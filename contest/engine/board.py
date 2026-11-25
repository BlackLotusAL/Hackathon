"""Static map data shared by the game, Bot protocol and replays."""
from dataclasses import dataclass
from functools import cached_property
from typing import NamedTuple, Optional

from .rules import PARAMS, RULESET


class Point(NamedTuple):
    x: int
    y: int


@dataclass(frozen=True)
class BoostZone:
    marker: Point
    cells: tuple[Point, ...]


def _points(values, label):
    if not isinstance(values, (list, tuple)) or any(
        not isinstance(point, (list, tuple))
        or len(point) != 2
        or any(type(coord) is not int for coord in point)
        for point in values
    ):
        raise ValueError(f'{label}坐标无效')
    return tuple(Point(*point) for point in values)


@dataclass(frozen=True)
class GameMap:
    id: str
    stage: str
    size: int
    terrain: tuple[str, ...]
    calib: tuple[Point, Point]
    boost_zones: tuple[BoostZone, ...]
    name: Optional[str] = None
    en: Optional[str] = None

    @classmethod
    def from_payload(cls, payload):
        """Decode a complete map and validate its cell data."""
        if not isinstance(payload, dict):
            raise ValueError('地图须为 JSON 对象')
        stage = payload.get('stage', RULESET)
        if stage != RULESET:
            raise ValueError('地图须使用当前赛题规则')
        map_id = payload.get('id')
        if not isinstance(map_id, str) or not map_id:
            raise ValueError('地图缺少编号')
        size, terrain = payload.get('size'), payload.get('terrain')
        if (size != PARAMS['size'] or type(size) is not int or not isinstance(terrain, (list, tuple))
                or len(terrain) != size or any(not isinstance(row, str) or len(row) != size for row in terrain)):
            raise ValueError('尺寸不是 41×41')
        if any(set(row) - {'.', '#', '*'} for row in terrain):
            raise ValueError('出现 . # * 以外的字符')
        calib = _points(payload.get('calib'), '校准区中心')
        if len(calib) != 2:
            raise ValueError('必须提供两个校准区中心')
        if any(not (1 <= x < size - 1 and 1 <= y < size - 1) for x, y in calib):
            raise ValueError('校准区出界')

        raw_zones = payload.get('boost_zones')
        if not isinstance(raw_zones, (list, tuple)) or len(raw_zones) != 3:
            raise ValueError('地图必须包含 3 个加速区')
        zones = []
        for number, zone in enumerate(raw_zones, 1):
            label = f'加速区 {number}'
            if not isinstance(zone, dict):
                raise ValueError(f'{label}必须包含标记点和覆盖格')
            marker, = _points([zone.get('marker')], f'{label}标记点')
            cells = _points(zone.get('cells'), f'{label}覆盖格')
            if marker not in cells:
                raise ValueError(f'{label}的标记点不在区内')
            if any(not (0 <= x < size and 0 <= y < size) or terrain[y][x] == '#' for x, y in cells):
                raise ValueError(f'{label}包含缺陷格或界外格')
            zones.append(BoostZone(marker, cells))
        if len({zone.marker for zone in zones}) != len(zones):
            raise ValueError('各加速区的标记点须互不重合')

        return cls(map_id, stage, size, tuple(terrain), calib, tuple(zones), payload.get('name'), payload.get('en'))

    def to_payload(self):
        """Serialize the same map for Bot initialization and replay."""
        payload = {
            'id': self.id, 'stage': self.stage, 'size': self.size,
            'terrain': list(self.terrain), 'calib': [list(point) for point in self.calib],
            'boost_zones': [
                {'marker': list(zone.marker), 'cells': [list(cell) for cell in zone.cells]}
                for zone in self.boost_zones
            ],
        }
        if self.name is not None:
            payload['name'] = self.name
        if self.en is not None:
            payload['en'] = self.en
        return payload

    @cached_property
    def tiles(self):
        return ''.join(self.terrain)

    @cached_property
    def base(self):
        owners = [-1] * (self.size * self.size)
        for seat, (cx, cy) in enumerate(self.calib):
            for y in range(cy - 1, cy + 2):
                for x in range(cx - 1, cx + 2):
                    owners[y * self.size + x] = seat
        return tuple(owners)

    @cached_property
    def _boost_cells(self):
        return frozenset(cell for zone in self.boost_zones for cell in zone.cells)

    def move_steps(self, x, y):
        return 2 if (x, y) in self._boost_cells else 1
