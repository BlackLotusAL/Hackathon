"""Rule parameters, directions, and match tiebreakers."""

DX = {'U': 0, 'D': 0, 'L': -1, 'R': 1}
DY = {'U': -1, 'D': 1, 'L': 0, 'R': 0}
OPP = {'U': 'D', 'D': 'U', 'L': 'R', 'R': 'L'}
DIRS = ('U', 'D', 'L', 'R')
TIEBREAK_METRICS = ('score', 'bonus', 'lead_turns', 'time_us')


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
    'order_first': 40,        # 第一个订单在第 40 回合末公示
    'order_every': 40,        # 之后每 40 回合公示一个
    'order_duration': 30,     # 公示后第 30 个回合末到期结算
    'order_bonus': 1,         # 到期时区域内每个己方图形区格奖励 1 倍价值
    'boost_every': 120,       # 加速道具刷新间隔 N：第 120 回合末起每 120 回合出现一个
    'boost_lead': 30,         # 提前 30 回合预告位置
    'boost_duration': 40,     # 拾取后加速持续 M 回合
    'startup_s': 10,          # 启动：GET /v1/health 须在 10 秒内返回就绪
    'init_ms': 2000,          # 开局请求时限
    'turn_ms': 200,           # 每回合请求时限
    'end_ms': 1000,           # 结束请求时限
    'max_errors': 10,         # 累计错误达到此数判负（规则第 9 节）
}
