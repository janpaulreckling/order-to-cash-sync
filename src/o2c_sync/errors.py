"""Error taxonomy.

Every connector translates library-specific exceptions into one of these, so the
engine can decide *what to do* without knowing *which system* failed:

- TransientError      -> retry with backoff (network blips, 5xx, expired session)
- PermanentError      -> do not retry; park the record in the dead-letter queue
- ConflictError       -> a business rule blocks the change; needs a human
- RateLimitExceeded   -> stop the whole run cleanly and resume next schedule
"""


class SyncError(Exception):
    """Base class for all sync errors."""


class TransientError(SyncError):
    """Temporary failure; safe to retry."""


class PermanentError(SyncError):
    """Retrying will not help (bad data, missing master data, 4xx)."""


class ConflictError(PermanentError):
    """Source and target disagree and a business rule says the target wins."""


class MappingError(PermanentError):
    """A source record could not be mapped onto the canonical model."""


class RateLimitExceeded(SyncError):
    """API budget exhausted. Abort the run and keep progress made so far."""
