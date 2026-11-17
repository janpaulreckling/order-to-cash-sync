import logging

from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from o2c_sync.errors import TransientError

log = logging.getLogger(__name__)


def retry_policy(max_attempts: int, max_wait: float = 30.0) -> Retrying:
    """Exponential backoff with jitter, only for errors classified as transient.

    Jitter matters: without it, parallel workers that failed together retry
    together and hammer a recovering API in lockstep.
    """
    return Retrying(
        retry=retry_if_exception_type(TransientError),
        wait=wait_exponential_jitter(initial=1, max=max_wait),
        stop=stop_after_attempt(max_attempts),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
