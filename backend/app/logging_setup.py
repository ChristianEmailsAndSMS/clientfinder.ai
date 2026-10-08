"""One place to configure logging for the API, scheduler and scripts."""
import logging

from .security_utils import RedactingFilter


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
    # httpx/httpcore log full request URLs (including ?api_key=...) at INFO. Never let them.
    for name in ("httpx", "httpcore", "anthropic", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
