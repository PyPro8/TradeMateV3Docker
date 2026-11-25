"""Application-level exceptions.

Future modules should raise subclasses of TradeMateError so the application
entry point can handle them in one place.
"""


class TradeMateError(Exception):
    """Base class for all expected TradeMate errors."""


class ConfigError(TradeMateError):
    """Configuration is missing, unreadable, or invalid."""


class BrokerError(TradeMateError):
    """Base class for broker-adapter errors."""


class BrokerUnavailableError(BrokerError):
    """The broker (or its bridge) cannot supply data right now."""


class OrderRejectedError(BrokerError):
    """The order was refused before it was sent, or the broker rejected it. Message is user-facing."""


class BrokerRejectedError(OrderRejectedError):
    """MT5/the broker received the request and REFUSED it. Carries the real retcode and broker explanation.

    Subclass of OrderRejectedError so existing handlers keep working; the API uses the type to tell a broker
    rejection ("order submitted: YES") apart from a refusal before sending ("order submitted: NO").
    """

    def __init__(self, message: str = "", retcode: int | None = None, broker_message: str = ""):
        super().__init__(message)
        self.retcode = retcode
        self.broker_message = broker_message


class CommandWithdrawnError(BrokerUnavailableError):
    """The command was written but MT5 never picked it up, and TradeMate removed it again.

    Provably nothing reached the broker. This is the only "no answer" case that may be reported as "not sent".
    """


class ExecutionUnconfirmedError(BrokerError):
    """A command was sent but no confirmation came back. The real outcome is UNKNOWN.

    ``execution_id`` points at the journal record that must be reconciled against the account.
    """

    def __init__(self, message: str = "", execution_id: str | None = None):
        super().__init__(message)
        self.execution_id = execution_id
