"""Run the Contest L0 suite with ``python -B -m tests``."""
import argparse
from pathlib import Path
import unittest


def cases_in(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases_in(item)
        else:
            yield item


def main(argv=None):
    parser = argparse.ArgumentParser(description='运行 Contest 的 L0 核心测试')
    parser.add_argument('--level', choices=('L0', 'all'), default='L0')
    parser.add_argument('--list', action='store_true', help='仅列出用例编号及对应测试')
    args = parser.parse_args(argv)
    directory = Path(__file__).resolve().parent
    loader = unittest.TestLoader()
    discovered = loader.discover(str(directory), top_level_dir=str(directory.parent))
    if loader.errors:
        parser.error('\n'.join(loader.errors))
    selected, identifiers = [], set()
    for test in cases_in(discovered):
        method = getattr(test, test._testMethodName)
        identifier = getattr(method, 'case_id', None)
        if identifier is None:
            parser.error(f'测试缺少用例编号: {test.id()}')
        if identifier in identifiers:
            parser.error(f'用例编号重复: {identifier}')
        identifiers.add(identifier)
        selected.append((identifier, test))
    if not selected:
        parser.error('没有可运行的 L0 用例')
    selected.sort(key=lambda entry: entry[0])
    if args.list:
        for identifier, test in selected:
            print(f'{identifier}  {test.id()}')
        return 0
    print(f'{args.level}: {len(selected)} cases', flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(test for _, test in selected))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
