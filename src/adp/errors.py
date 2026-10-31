"""Domain errors."""


class AdpError(Exception):
    """Base class for ADP domain errors."""


class ConfigError(AdpError):
    """Invalid or missing configuration."""
