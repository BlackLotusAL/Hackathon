from time import sleep

from sdk import InitState, TurnState


class Strategy:
    def __init__(self, _init: InitState):
        pass

    def act(self, state: TurnState) -> str:
        sleep(0.35)  # Exceed the 200 ms turn deadline.
        return state.me.dir
