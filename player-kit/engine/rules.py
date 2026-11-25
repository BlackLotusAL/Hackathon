"""Rule parameters, directions, and match tiebreakers."""

from .prelim import STATION_PERIOD, TIEBREAK_METRICS

RULESET = 'prelim'


DX = {'U': 0, 'D': 0, 'L': -1, 'R': 1}
DY = {'U': -1, 'D': 1, 'L': 0, 'R': 0}
OPP = {'U': 'D', 'D': 'U', 'L': 'R', 'R': 'L'}
DIRS = ('U', 'D', 'L', 'R')


def valid_command(cmd):
    return isinstance(cmd, str) and cmd in DIRS

def compare_metrics(metrics):
    """Return the winning side index and the first differing metric."""
    for key in TIEBREAK_METRICS:
        a, b = metrics[key]
        if a != b:
            return int(b < a) if key == 'time_us' else int(b > a), key
    return None, None

# 与规则文档各节的参数一致
PARAMS = {
    'size': 41,               # 基板边长
    'max_turns': 600,         # 回合上限
    'restart': 5,             # 轮廓报废后停机校准回合
    'key_value': 3,           # 关键图形区价值
    'station_period': STATION_PERIOD,
    'startup_s': 10,          # 启动：GET /v1/health 须在 10 秒内返回就绪
    'init_ms': 2000,          # 开局请求时限
    'turn_ms': 200,           # 每回合请求时限
    'end_ms': 1000,           # 结束请求时限
    'max_errors': 10,         # 累计错误达到此数判负（规则第 9 节）
}
