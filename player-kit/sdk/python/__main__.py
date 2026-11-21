"""Start the HTTP service for a Python Bot."""
import argparse
from importlib import import_module
import os
from pathlib import Path
import sys

from .http import run
from .strategy import StrategyRunner


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m sdk.python', description='启动 Python 策略的 Bot 服务')
    parser.add_argument('directory', type=Path, help='包含 strategy.py 的提交目录')
    parser.add_argument('--port', type=int)
    args = parser.parse_args(argv)
    directory = args.directory.expanduser().resolve()
    if not (directory / 'strategy.py').is_file():
        parser.error(f'{directory} 缺少 strategy.py')
    os.chdir(directory)
    sys.path.insert(0, str(directory))
    strategy = import_module('strategy').Strategy
    run(runner_factory=lambda: StrategyRunner(strategy), name=directory.name, port=args.port)


if __name__ == '__main__':
    main()
