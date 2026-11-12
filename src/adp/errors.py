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


class SourceSpecError(AdpError):
    """Invalid source specification (YAML syntax, field or registry error)."""


class LLMError(AdpError):
    """LLM provider failure, invalid LLM output or privacy guard violation.

    `input_tokens`/`output_tokens` carry what failed attempts consumed, so spend is still
    counted against the budget. Messages never contain prompts, data values or keys.
    """

    def __init__(self, message: str, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class MappingError(AdpError):
    """Mapping decision or human review failure (messages carry no data value)."""


class TransformError(MappingError):
    """Invalid transformation rule or value that cannot be transformed (no data value inside)."""
