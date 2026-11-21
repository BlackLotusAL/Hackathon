"""Translate wire messages into Python strategy callbacks."""
from .state import InitState, TurnState


class StrategyRunner:
    """Construct a strategy from typed init data and pass it typed turns."""

    def __init__(self, factory):
        self.factory = factory
        self.init_message = None
        self.init = None
        self.strategy = None

    def handle(self, msg):
        kind = msg['type']
        if kind == 'init':
            self.init_message = msg
            self.init = InitState.from_message(msg)
            self.strategy = self.factory(self.init)
        elif kind == 'turn':
            state = TurnState.from_message(self.init, self.init_message, msg)
            return {'turn': state.turn, 'cmd': self.strategy.act(state)}
        elif hasattr(self.strategy, 'end'):
            self.strategy.end(msg)
        return None
