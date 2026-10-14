"""Expected, user-readable failures at the prototype's boundaries."""


class SpliceError(Exception):
    pass


class ContractError(SpliceError):
    pass


class PermissionDenied(SpliceError):
    pass


class PlanningError(SpliceError):
    pass


class ExecutionError(SpliceError):
    pass


class ConfirmationRequired(SpliceError):
    pass


class GateRejected(SpliceError):
    pass
