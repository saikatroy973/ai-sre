class InvestigatorError(Exception):
    """An error safe to translate into a generic operator-facing response."""


class ConfigurationError(InvestigatorError):
    pass


class AuthorizationError(InvestigatorError):
    pass


class NotFoundError(InvestigatorError):
    pass


class BusyError(InvestigatorError):
    pass


class SwitchError(InvestigatorError):
    """A safe Switch failure. Never carries response bodies or credentials."""


class ReasoningError(InvestigatorError):
    def __init__(self, code: str, *, usage=None):
        self.code = code
        self.usage = usage
        super().__init__(code)
