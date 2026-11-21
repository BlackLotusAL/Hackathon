"""Serve the Contest REST contract or inspect and recover persisted executions."""
import argparse
from getpass import getuser
from pathlib import Path

from engine.protocol import dumps

from .application import Contest
from .config import Settings
from .server import serve


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m service', description=__doc__)
    parser.add_argument('--config', type=Path, help='JSON 配置文件；归档路径相对于 Contest 目录，其他路径相对于配置文件目录')
    parser.add_argument('--db', type=Path, help='覆盖配置中的数据库路径')
    commands = parser.add_subparsers(dest='action', required=True)
    server = commands.add_parser('serve', help='启动 Contest HTTP 服务')
    server.add_argument('--archive-root', type=Path)
    server.add_argument('--maps', type=Path, help='含已公布哈希的正式地图池配置')
    server.add_argument('--arena-url')
    server.add_argument('--host', help='覆盖配置中的监听 IP，默认 0.0.0.0')
    server.add_argument('--port', type=int, help='覆盖配置中的端口，默认 8082')
    commands.add_parser('show', help='查看一场对决的结算及执行尝试').add_argument('duel_id')
    commands.add_parser('recheck', help='重新复核暂缓生效的执行').add_argument('run_id')
    commands.add_parser('retry', help='为已作废的预定局创建下一次尝试').add_argument('run_id')
    commands.add_parser('resend', help='按原内容补发未确认的 Arena 执行请求').add_argument('run_id')
    void = commands.add_parser('void', help='记录组织方确认的平台故障并作废')
    void.add_argument('run_id')
    void.add_argument('reason')
    commands.add_parser('rulings', help='列出待主办方裁定的局和场次')
    rule = commands.add_parser('rule', help='为待裁定的局或场次录入胜方')
    rule.add_argument('scope', choices=('game', 'duel'))
    rule.add_argument('target', help='单局执行编号或整场对决编号')
    rule.add_argument('winner', help='裁定胜方的参赛方编号')
    rule.add_argument('reason')
    args = parser.parse_args(argv)
    try:
        settings = Settings.load(args.config)
        if args.db is not None:
            settings = settings.override(db=args.db)
        if args.action == 'serve':
            names = ('archive_root', 'maps', 'arena_url', 'host', 'port')
            overrides = {name: getattr(args, name) for name in names if getattr(args, name) is not None}
            serve(settings.override(**overrides))
        else:
            if settings.db is None:
                raise ValueError('请在配置文件提供 db，或通过 --db 指定数据库')
            contest = Contest(settings.db)
            if args.action == 'show':
                print(dumps(contest.duels.show(args.duel_id)))
            elif args.action == 'retry':
                print(contest.runs.retry(args.run_id))
            elif args.action == 'recheck':
                contest.runs.recheck(args.run_id)
            elif args.action == 'resend':
                contest.runs.resend(args.run_id)
            elif args.action == 'void':
                contest.runs.invalidate(args.run_id, args.reason)
            elif args.action == 'rulings':
                print(dumps(contest.rulings.pending()))
            elif args.action == 'rule':
                print(dumps(contest.rulings.record(args.scope, args.target, args.winner, args.reason, operator=getuser())))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
