"""Play a single local match using the Bot protocol."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import secrets

from .adjudication import TIME_LIMIT_STATES, apply_startup, apply_turn
from .game import Game
from .launch import ProcessBot
from .protocol import end_message, init_message, turn_message
from .replay import Recorder
from .rules import PARAMS


def play_match(board, submission, opponent, *, replay_path=None):
    game = Game(board)
    recorder = Recorder(game, submission, opponent) if replay_path is not None else None
    bots = (ProcessBot(submission), ProcessBot(opponent))
    run_id = f'run-{secrets.randbits(64)}.1'
    errors = [0, 0]
    turn_limit = PARAMS['turn_ms'] / 1000

    with ExitStack() as resources:
        for bot in bots:
            resources.callback(bot.close)
        pool = resources.enter_context(ThreadPoolExecutor(2))

        starting = [pool.submit(bot.start, PARAMS['startup_s']) for bot in bots]
        ready, startup = zip(*(request.result() for request in starting))
        initial = [None, None]
        if all(ready):
            opening = [
                pool.submit(bot.post, run_id, 'init', init_message(game, seat), PARAMS['init_ms'] / 1000)
                for seat, bot in enumerate(bots)
            ]
            initial = [request.result()[0] for request in opening]
        exits = [bot.exited() for bot in bots]
        apply_startup(game, errors, startup, initial, exits)
        if recorder is not None:
            recorder.observe(phase='startup', startup=list(startup), init=initial, exits=exits)

        while not game.done:
            message = turn_message(game)
            pending = [pool.submit(bot.turn, run_id, message, turn_limit) for bot in bots]
            commands, statuses, elapsed_us = zip(*(request.result() for request in pending))
            counted_us = [PARAMS['turn_ms'] * 1000 if status in TIME_LIMIT_STATES else elapsed
                          for status, elapsed in zip(statuses, elapsed_us)]
            exits = [bot.exited() for bot in bots]
            apply_turn(game, errors, statuses, counted_us, exits)
            if recorder is not None:
                recorder.observe(turn=message['turn'], status=list(statuses), us=counted_us, exits=exits)
            if not game.done:
                events = game.step(commands)
                if recorder is not None:
                    recorder.record(commands, events, counted_us)

        message = end_message(game)
        closing = [
            pool.submit(bot.post, run_id, 'end', message, PARAMS['end_ms'] / 1000)
            for bot, started in zip(bots, ready) if started and not bot.exited()
        ]
        for request in closing:
            request.result()

    if recorder is not None:
        recorder.save(replay_path, errors)
    return game, errors
