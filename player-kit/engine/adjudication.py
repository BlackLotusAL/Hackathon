"""Apply the technical-forfeit rules to Bot responses."""
from .rules import PARAMS


TURN_ERRORS = ('timeout', 'connection', 'invalid')
TIME_LIMIT_STATES = ('timeout', 'connection')


def _forfeit(game, failures):
    losers = [seat for seat, reason in enumerate(failures) if reason]
    if len(losers) == 1:
        loser = losers[0]
        game.forfeit(loser, failures[loser])
    elif losers:
        game.forfeit(-1, 'crash' if 'crash' in failures else failures[0])


def apply_startup(game, errors, startup, initial, exits):
    failures = []
    for seat, (state, init, exited) in enumerate(zip(startup, initial, exits)):
        failure = None
        if exited or state == 'crash' or init == 'crash':
            failure = 'crash'
        elif state != 'ready':
            failure = 'startup'
        elif init is not None and init != 'ok':
            errors[seat] += 1
        failures.append(failure)
    _forfeit(game, failures)


def apply_turn(game, errors, statuses, us, exits):
    failures = []
    for seat, (status, elapsed, exited) in enumerate(zip(statuses, us, exits)):
        # Both issued requests count, including the round that triggers a forfeit.
        game.time_us[seat] += elapsed
        if status in TURN_ERRORS:
            errors[seat] += 1
        if exited or status == 'crash':
            failures.append('crash')
        else:
            failures.append('errors' if errors[seat] >= PARAMS['max_errors'] else None)
    _forfeit(game, failures)
