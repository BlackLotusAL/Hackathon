from sdk import InitState, TurnState


class Strategy:
    def __init__(self, _init: InitState):
        pass

    def act(self, _state: TurnState) -> str:
        return 'X'  # The SDK rejects this command.
