"""Domain errors."""


class AdpError(Exception):
    """Base class for ADP domain errors."""


class ConfigError(AdpError):
    """Invalid or missing configuration."""


class SchemaError(AdpError):
    """Schema detection, mapping or evolution failure."""


class QualityError(AdpError):
    """Data quality rule violation."""


class IngestionError(AdpError):
    """Source ingestion failure."""
