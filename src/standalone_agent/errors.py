"""Public failure categories, independent of provider transports."""


class ConversationError(ValueError):
    """Never put provider text, credentials or user data in a public failure."""

    def __init__(self, message, code="agent_error", *, diagnostic=None):
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic
