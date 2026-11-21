"""Run an Arena judge task with ``python -m judge``."""
import argparse
from pathlib import Path

from engine.protocol import dumps

from .task import run_task


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m judge', description='执行 Arena 下发的单局裁判任务')
    parser.add_argument('--task', type=Path, default=Path('/task/task.json'))
    parser.add_argument('--out', type=Path, default=Path('/out'))
    parser.add_argument('--stage', choices=('prelim', 'final'), help='覆盖 task.json 的 rules，缺省为 prelim')
    args = parser.parse_args(argv)
    try:
        print(dumps(run_task(args.task, args.out, args.stage)))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
