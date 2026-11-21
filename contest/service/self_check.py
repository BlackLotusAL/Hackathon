"""Exercise the current Bot HTTP protocol with an isolated practice session."""
from ipaddress import ip_address
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

from engine.core import Game, PARAMS
from engine.mapgen import generate
from engine.protocol import end_message, init_message, turn_message
from engine.referee import HttpBot

from .domain import BATTLE_RULES, ContestError


TRIAL_TURNS = 60


def _local_bot(target):
    try:
        url = urlsplit(target)
        address = ip_address('127.0.0.1' if url.hostname == 'localhost' else url.hostname)
        if not address.is_loopback or url.username is not None or url.port == 0:
            raise ValueError()
        bot = HttpBot(target)
    except ValueError as error:
        raise ContestError(400, 'INVALID_CHECK_TARGET', '请填写本机 Bot 的 HTTP(S) 根地址') from error
    bot.host = str(address)
    return bot


def _http_detail(status, code):
    return f'HTTP {code}' if code else {'timeout': '响应超时', 'connection': '连接失败'}.get(status, '响应无效')


def _trial(bot, run_id, game, record):
    errors, total_us = 0, 0
    for turn in range(TRIAL_TURNS):
        command, status, _, elapsed_us = bot.turn(run_id, turn_message(game), PARAMS['turn_ms'] / 1000)
        errors += status != 'ok'
        total_us += elapsed_us
        if turn == 0:
            record('回合响应', 'ok' if status == 'ok' else 'fail', command or _http_detail(status, 0))
        if errors and (turn == 0 or errors >= PARAMS['max_errors']):
            game.forfeit(0, 'errors')
            break
        game.step((command, None))

    mean_ms = total_us / (turn + 1) / 1000
    level = 'fail' if errors else 'warn' if mean_ms > PARAMS['turn_ms'] * 0.7 else 'ok'
    record('试打 60 回合', level, f'{game.turn} 回合，错误 {errors} 次，平均 {mean_ms:.1f} 毫秒')


def check_bot(body):
    rules, target = body.get('rules'), body.get('target')
    if body.keys() != {'rules', 'target'} or rules not in BATTLE_RULES:
        raise ContestError(400, 'INVALID_RULES', '请选择初赛或决赛规则并填写 Bot 地址')
    if not isinstance(target, str) or not target.strip():
        raise ContestError(400, 'INVALID_CHECK_TARGET', '请填写 Bot 服务地址')
    target = target.strip()
    bot = _local_bot(target)
    items = []

    def record(name, level, detail):
        items.append({'name': name, 'level': level, 'detail': detail})

    report = {'name': target, 'rules': rules, 'items': items}
    started = monotonic()
    ready, _ = bot.start(PARAMS['startup_s'])
    if not ready:
        record('启动就绪', 'fail', 'Bot 未返回就绪状态')
        return report
    record('启动就绪', 'ok', f'{monotonic() - started:.2f} 秒就绪')

    run_id = f'run-{uuid4().int}.1'
    game = Game(generate(42, stage=rules), stage=rules, max_turns=TRIAL_TURNS)
    try:
        status, code, _, _ = bot.post(run_id, 'init', init_message(game, 0), PARAMS['init_ms'] / 1000)
        record('开局请求', 'ok' if status == 'ok' else 'fail', _http_detail(status, code))
        if status == 'ok':
            status, code, _, _ = bot.request(
                'POST', f'/v1/matches/run-{uuid4().int}.1/turns/1', turn_message(game), 1,
            )
            record('未知对局', 'ok' if status == 'ok' and code == 404 else 'warn', _http_detail(status, code))
            _trial(bot, run_id, game, record)
    finally:
        if not game.done:
            game.forfeit(0, 'stopped')
        status, code, _, _ = bot.post(run_id, 'end', end_message(game), PARAMS['end_ms'] / 1000)
        record('结束通知', 'ok' if status == 'ok' else 'warn', _http_detail(status, code))

    return report
