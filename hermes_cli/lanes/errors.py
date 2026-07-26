"""Typed lane-framework failures."""


class LaneError(RuntimeError):
    pass


class LaneNotFound(LaneError):
    pass


class LaneNotEnabledError(LaneError):
    pass


class LaneModuleNotFound(LaneError):
    pass


class LaneRateLimitExceeded(LaneError):
    pass


class ApprovalNotGranted(LaneError):
    pass


class ApprovalExpired(LaneError):
    pass


class PublishDisabled(LaneError):
    pass


__all__ = [
    "ApprovalExpired",
    "ApprovalNotGranted",
    "LaneError",
    "LaneModuleNotFound",
    "LaneNotEnabledError",
    "LaneNotFound",
    "LaneRateLimitExceeded",
    "PublishDisabled",
]
