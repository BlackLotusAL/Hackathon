"""Run Exposure Field practice commands from the player kit."""
import argparse
from pathlib import Path
import sys
import time

from backend.practice.catalog import DEFAULT_BOT, generate_maps
from backend.practice.check import check
from backend.practice.match import prepare_match
from backend.practice.replay_store import verify_replay_file
from backend.practice.tournament import BatchTournament
from backend.runtime.docker import build_image
from game_core import mapgen
from game_core.protocol import dumps


LEVEL = {'ok': '通过', 'warn': '提醒', 'fail': '失败'}
REASON = {'limit': '得分', 'bonus': '累计订单奖励', 'lead_turns': '领先回合数', 'time': '累计响应时间',
          'tie': '四项指标全同', 'crash': '程序退出', 'errors': '累计 10 次错误',
          'startup': '启动未就绪', 'stopped': '手动停止'}


def _print_frame(frame, _entry):
    if frame and frame['turn'] % 100 == 0:
        print('  回合 %d  得分 %d : %d' % (frame['turn'], *frame['score']), file=sys.stderr)


def _match(args):
    match = prepare_match([args.p0, args.p1], map_name=args.map, output=args.out,
                          data_dir=args.data_dir, timing=not args.no_timing, on_frame=_print_frame)
    if args.map in (None, '', 'random'):
        print(f"地图：{match.board['id']}（随机生成；复现这一局用 --map {match.board['id']}）")
    started = time.perf_counter()
    result = match.run()
    players = result['players']
    winner = result['winner']
    outcome = f'{winner} 号方 {players[winner]} 获胜' if winner in (0, 1) else '待判'
    reason = REASON.get(result['reason'], result['reason'])
    print(f"地图 {result['map']}（{result['stage']}）  {players[0]} vs {players[1]}")
    print('结果：%s（%s）  比分 %d : %d  订单奖励 %d / %d  领先回合 %d / %d  共 %d 回合' % (
        outcome, reason, *result['score'], *result['bonus'], *result['lead_turns'], result['turns']))
    print('错误次数：%d / %d   累计响应时间 %.3f / %.3f 秒   用时 %.1f 秒' % (
        *result['errors'], result['time_us'][0] / 1e6, result['time_us'][1] / 1e6, time.perf_counter() - started))
    print(f'输出：{match.out_dir}（replay.json 可在 python -m exposure serve 的对局页查看或导入）')


def _tournament(args):
    map_ids = args.map_list.split(',') if args.map_list else None
    tournament = BatchTournament(args.bots, map_ids=map_ids, count=args.maps,
                                 seed=args.seed, fast=args.fast, workers=args.jobs)
    total = len(tournament.jobs)
    mode = '（进程内、放宽时限）' if tournament.fast else ''
    print(f"{len(tournament.names)} 个 Bot，{len(tournament.map_ids)} 张地图（{' '.join(tournament.map_ids)}），"
          f"共 {total} 局，{tournament.workers} 个进程并行{mode}……", flush=True)

    def progress(completed):
        if completed % 10 == 0:
            print(f'  已完成 {completed} / {total}', flush=True)

    started = time.perf_counter()
    records, detail = tournament.run(args.data_dir, on_progress=progress)
    print('\n两两对战（行对列的胜-负，括号内为平均分差）')
    print(' ' * 14 + ''.join(f'{name:<20}' for name in tournament.names))
    for name, row in zip(tournament.names, tournament.head_to_head(records)):
        labels = ['—' if cell is None else '%d-%d (%+.0f)' % cell for cell in row]
        print(f'{name:<14}' + ''.join(f'{label:<20}' for label in labels))
    undecided = sum(result['winner'] is None for result in records)
    errors = sum(sum(result['errors']) for result in records)
    print(f'\n待判 {undecided} 局，错误 {errors} 次，用时 {time.perf_counter() - started:.0f} 秒')
    print(f'明细：{detail}')


def _check(args):
    items, name = check(args.target, quick=args.quick)
    print(f'自检 {name}')
    for label, level, detail in items:
        print(f'  [{LEVEL[level]}] {label}  {detail}')
    return 1 if any(level == 'fail' for _, level, _ in items) else 0


def _build(args):
    build_image(args.directory, args.tag)


def _verify(args):
    result = verify_replay_file(args.replay, map_file=args.map_file, map_sha256=args.map_sha256)
    print(dumps(result))


def _generate(args):
    maps = generate_maps(args.seed, args.count, args.out)
    if args.out is not None:
        print(f"已保存 {len(maps)} 张地图到 {args.out}：{' '.join(board['id'] for board in maps)}")
        return
    for board in maps:
        defects = sum(row.count('#') for row in board['terrain'])
        keys = sum(row.count('*') for row in board['terrain'])
        boosts = ' '.join(f'({x},{y})' for x, y in board['boost_points'])
        print(f"{board['id']}  缺陷 {defects} 格  关键图形区 {keys} 格  "
              f"订单区域 {len(board['orders'])} 个  道具刷新点 {boosts}")
        print(mapgen.draw(board) + '\n')


def _serve(args):
    from backend.practice.server import serve

    serve(host=args.host, port=args.port, open_browser=args.open, data_dir=args.data_dir)


def _parser():
    parser = argparse.ArgumentParser(
        prog='python -m exposure', description='曝光领地本地工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=('示例\n'
                '  python -m exposure serve --open\n'
                f'  python -m exposure check {DEFAULT_BOT}\n'
                f'  python -m exposure match {DEFAULT_BOT} bots/bot002 --map R7\n'),
    )
    commands = parser.add_subparsers(metavar='命令')
    data = argparse.ArgumentParser(add_help=False)
    data.add_argument('--data-dir', type=Path, help='本地数据目录，默认读取 ARENA_DATA_DIR 或 ~/.exposure-field/practice')

    command = commands.add_parser('serve', parents=[data], help='启动本地练习网页')
    command.add_argument('--host', default='127.0.0.1')
    command.add_argument('--port', type=int, default=8080)
    command.add_argument('--open', action='store_true', help='启动后用浏览器打开')
    command.set_defaults(run=_serve)

    command = commands.add_parser('check', help='接口自检')
    command.add_argument('target', nargs='?', default=DEFAULT_BOT,
                         help=f'提交目录、启动命令或 HTTP(S) 地址，默认 {DEFAULT_BOT}')
    command.add_argument('--quick', action='store_true', help='只查接口，不试打')
    command.set_defaults(run=_check)

    command = commands.add_parser('build', help='构建 C++ 或 Java 策略镜像')
    command.add_argument('directory', help='提交目录')
    command.add_argument('-t', '--tag', required=True, help='Docker 镜像标签')
    command.set_defaults(run=_build)

    command = commands.add_parser('verify-replay', help='按同版引擎重算正式回放')
    command.add_argument('replay', type=Path, help='从平台下载的 replay.json')
    command.add_argument('--map-file', type=Path, help='阶段结束后公开的封存地图原文件')
    command.add_argument('--map-sha256', help='赛前公告的封存地图 SHA-256')
    command.set_defaults(run=_verify)

    command = commands.add_parser('match', parents=[data], help='打一局')
    command.add_argument('p0', help='0 号方（蓝），可填 Bot 目录、启动命令、HTTP(S) 地址或 inproc:目录')
    command.add_argument('p1', help='1 号方（红）')
    command.add_argument('--map', help='R<种子> 为随机地图，例如 R7。不填则随机')
    command.add_argument('--out', type=Path, help='指定本局输出目录，默认在本地数据目录创建')
    command.add_argument('--no-timing', action='store_true', help='不执行时限（断点调试用）')
    command.set_defaults(run=_match)

    command = commands.add_parser('tournament', parents=[data], help='循环赛')
    command.add_argument('bots', nargs='+', help='参赛的 Bot 目录')
    command.add_argument('--maps', type=int, default=6, help='随机地图张数，默认 6。每张图交换座位各打一局')
    command.add_argument('--map-list', help='指定地图，逗号分隔，例如 R7,R42')
    command.add_argument('--seed', type=int, help='地图总种子，用于复现同一组地图')
    command.add_argument('--fast', action='store_true', help='在进程内运行 Python 策略并放宽时限')
    command.add_argument('--jobs', type=int, help='并行局数，计时模式默认 1，快速模式默认 CPU 核数')
    command.set_defaults(run=_tournament)

    command = commands.add_parser('mapgen', help='生成随机地图')
    command.add_argument('--seed', type=int, help='起始种子。不填则随机')
    command.add_argument('--count', type=int, default=1)
    command.add_argument('--out', type=Path, help='保存为 JSON 文件。不填则在终端里画出来')
    command.set_defaults(run=_generate)
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if not hasattr(args, 'run'):
        parser.print_help()
        return 0
    try:
        return args.run(args) or 0
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
