"""Validate submission files and exercise the Bot HTTP endpoints."""
import os
from pathlib import Path
import secrets
import time

from game_core import mapgen
from game_core.protocol import end_message, init_message, turn_message
from game_core.rules import Game, PARAMS

from ..runtime.local import ManagedBot, bot_name
from ..runtime.submission import bot_dir, submission_language
from .match import Match


def check_bot(spec, cwd=None, quick=False):
    items = []
    board = mapgen.generate(20261101)
    bot = ManagedBot(spec, cwd=cwd)
    try:
        t0 = time.monotonic()
        ready, reason = bot.start(PARAMS['startup_s'])
        elapsed = time.monotonic() - t0
        if not ready:
            detail = '程序退出' if reason == 'crash' else '10 秒内未就绪或鉴权失败'
            return [('启动就绪', 'fail', detail)]
        items.append(('启动就绪', 'ok', f'{elapsed:.2f} 秒就绪'))
        if bot.proc is not None:
            token = bot.token
            try:
                bot.token = 'wrong-' + secrets.token_hex(3)
                status, code, _, _ = bot.request('GET', '/v1/health', None, 1.0)
            finally:
                bot.token = token
            items.append(('令牌校验', 'ok' if status == 'ok' and code in (401, 403) else 'fail',
                          f'错误令牌得到 {code or status}'))

        match_id = 'check-' + secrets.token_hex(3)
        game = Game(board)
        status, code, _, _ = bot.post(match_id, 'init', init_message(game, 0), PARAMS['init_ms'] / 1000)
        items.append(('开局请求', 'ok' if status == 'ok' else 'fail', f'HTTP {code or status}'))
        message = turn_message(game)
        command, status, raw, _ = bot.turn(match_id, message, PARAMS['turn_ms'] / 1000)
        items.append(('回合响应', 'ok' if status == 'ok' else 'fail', command or f'{status}: {raw}'))
        status, code, _, _ = bot.request('POST', f'/v1/matches/{match_id}-unknown/turns/1', message, 1.0)
        items.append(('未知对局', 'ok' if status == 'ok' and code == 404 else 'warn', f'HTTP {code or status}'))
        game.forfeit(1, 'stopped')
        status, code, _, _ = bot.post(match_id, 'end', end_message(game), PARAMS['end_ms'] / 1000)
        items.append(('结束通知', 'ok' if status == 'ok' else 'warn', f'HTTP {code or status}'))

        if not quick and not any(level == 'fail' for _, level, _ in items):
            turns = 60
            result = Match(board, [bot, 'inproc:bots/bot001'],
                           names=[bot_name(spec, cwd), 'bot001'], max_turns=turns).run()
            errors = result['errors'][0]
            success = result['turns'] == turns and errors == 0
            mean_ms = result['time_us'][0] / 1000 / max(1, result['turns'])
            level = 'fail' if not success else 'warn' if mean_ms > PARAMS['turn_ms'] * 0.7 else 'ok'
            items.append(('试打 60 回合', level, f"{result['turns']} 回合，错误 {errors} 次，平均 {mean_ms:.1f} 毫秒"))
    finally:
        bot.close()
    return items


def check(target, cwd=None, quick=False):
    """Check a directory's submission files, or exercise a local command or URL."""
    cwd = cwd or os.getcwd()
    directory = bot_dir(target, cwd)
    if directory is not None:
        name = Path(directory).name
        try:
            language = submission_language(directory)
        except (OSError, ValueError) as error:
            return [('提交结构', 'fail', str(error))], name
        items = [('提交结构', 'ok', f'{language} 提交文件齐全')]
        spec = directory
    else:
        path = Path(cwd) / target
        if path.suffix.lower() == '.zip' and path.is_file():
            return [('自检目标', 'fail', '请填写包含 bot.json 的提交目录')], target
        name, spec, items = target, target, []

    try:
        items.extend(check_bot(spec, cwd, quick))
    except (OSError, ValueError) as error:
        items.append(('启动 Bot', 'fail', str(error)))
    return items, name
