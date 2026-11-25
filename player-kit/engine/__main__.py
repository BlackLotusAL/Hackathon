"""Play one local match against a bundled baseline Bot."""
import argparse
from datetime import datetime
from pathlib import Path
import secrets
import sys

from sdk.submission import KIT_ROOT

from .maps import generate
from .match import play_match
from .replay import write_replay_index


BOTS_DIR = KIT_ROOT / 'bots'
REASONS = {
    'limit': '得分',
    'lead_turns': '领先回合数',
    'time': '累计响应时间',
    'tie': '三项指标相同，待裁定',
    'double_loss': '双方同时技术判负',
    'startup': 'Bot 未就绪',
    'crash': 'Bot 退出',
    'errors': '累计错误达到上限',
}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m engine', description='与基准 Bot 试打一局')
    parser.add_argument('bot', type=Path, help='包含 bot.json 的提交目录')
    parser.add_argument('--opponent', choices=['bot001', 'bot002', 'bot003'], default='bot002',
                        help='基准 Bot，默认 bot002')
    parser.add_argument('--seed', type=int, default=42, help='地图种子，默认 42')
    args = parser.parse_args(argv)

    submission = args.bot.expanduser().resolve()
    players = [submission.name, args.opponent]
    try:
        board = generate(args.seed)
        print(f'地图 {board["id"]} · {players[0]} 对 {players[1]}', flush=True)
        name = f'{datetime.now():%Y%m%d-%H%M%S}-{board["id"]}-{secrets.token_hex(4)}.json'
        replay_path = Path('replays') / name
        game, errors = play_match(board, submission, BOTS_DIR / args.opponent, replay_path=replay_path)
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')
    except KeyboardInterrupt:
        parser.exit(130, '对局已停止\n')

    print(f'完成 {game.turn}/{game.max_turns} 回合')
    for seat, name in enumerate(players):
        print(f'  {seat} 号方 {name}  得分 {game.score(seat)}'
              f'  领先 {game.lead_turns[seat]} 回合  响应时间 {game.time_us[seat]} 微秒  错误 {errors[seat]} 次')
    outcome = '未判定胜方' if game.winner is None else f'{game.winner} 号方 {players[game.winner]} 获胜'
    if game.reason == 'double_loss':
        outcome = '双方各记一负'
    print(f'{outcome}（{REASONS[game.reason]}）')
    print(f'回放已保存至 {replay_path}')
    try:
        write_replay_index(replay_path.parent)
    except OSError as error:
        print(f'无法更新回放列表（{error}）', file=sys.stderr)
    return int(game.turn < game.max_turns)


if __name__ == '__main__':
    raise SystemExit(main())
