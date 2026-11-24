import os

from sdk import InitState, TurnState


class Strategy:
    def __init__(self, _init: InitState):
        pass

    def act(self, state: TurnState) -> str:
        if state.turn >= 3:
            # Terminate the whole Bot process from the SDK's request thread.
            os._exit(1)
        return state.me.dir
