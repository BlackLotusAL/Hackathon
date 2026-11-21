"""Generate maps and verify replays with ``python -m engine``."""
import argparse
import json
from pathlib import Path
import random

from . import mapgen
from .protocol import dumps, json_object
from .replay import game_result, verify_replay


def generate_maps(args):
    seeds = ([args.seed + index for index in range(args.count)] if args.seed is not None
             else [random.randrange(1, 10 ** 6) for _ in range(args.count)])
    maps = [mapgen.generate(seed, args.stage) for seed in seeds]
    if args.out:
        with args.out.open('w', encoding='utf-8') as output:
            json.dump(maps, output, ensure_ascii=False, indent=1)
        print('已保存 %d 张地图到 %s：%s' % (len(maps), args.out, ' '.join(board['id'] for board in maps)))
        return
    for board in maps:
        defects = sum(row.count('#') for row in board['terrain'])
        keys = sum(row.count('*') for row in board['terrain'])
        print('%s  缺陷 %d 格  关键图形区 %d 格  订单区域 %d 个  道具刷新点 %s' % (
            board['id'], defects, keys, len(board['orders']),
            ' '.join('(%d,%d)' % tuple(point) for point in board['boost_points'])))
        print(mapgen.draw(board) + '\n')


def verify_recording(args):
    game = verify_replay(json_object(args.replay.read_bytes()))
    print(dumps(game_result(game)))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m engine', description='曝光领地规则引擎工具')
    commands = parser.add_subparsers(dest='command', required=True)
    maps = commands.add_parser('mapgen', help='生成随机地图')
    maps.add_argument('--seed', type=int, default=None, help='起始种子；不填则随机')
    maps.add_argument('--count', type=int, default=1)
    maps.add_argument('--stage', default='prelim', choices=['prelim', 'final'])
    maps.add_argument('--out', type=Path, help='保存为 JSON 文件；不填则在终端里画出来')
    maps.set_defaults(handler=generate_maps)
    replay = commands.add_parser('verify-replay', help='独立复核正式回放或初赛材料包回放')
    replay.add_argument('replay', type=Path)
    replay.set_defaults(handler=verify_recording)
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
