"""Select functional priorities with ``python -B contest/tests --level L0``."""
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
    parser = argparse.ArgumentParser(description='按功能重要程度运行 Contest 测试')
    parser.add_argument('--level', choices=('L0', 'L1', 'L2', 'all'), default='L0')
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
            parser.error(f'测试未分级: {test.id()}')
        if identifier in identifiers:
            parser.error(f'用例编号重复: {identifier}')
        identifiers.add(identifier)
        if args.level == 'all' or identifier.startswith(args.level + '-'):
            selected.append((identifier, test))
    if not selected:
        parser.error(f'{args.level} 没有可运行的用例')
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
