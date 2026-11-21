"""Organizer-owned entry point for a Python strategy."""
import sys
from pathlib import Path


def main():
    sdk_root = Path(__file__).resolve().parents[2]
    submission = Path(sys.argv[1]).resolve()
    sys.path[:0] = [str(sdk_root), str(submission)]
    from backend.runtime.sdk import StrategyRunner, run
    from strategy import Strategy

    run(runner_factory=lambda: StrategyRunner(Strategy), name=submission.name)


if __name__ == '__main__':
    main()
