"""Browser creation and session lifecycle."""

from app.browser.factory import BrowserFactory, context_options, launch_options
from app.browser.session import classify_navigation_error, run_session, summarize_error

__all__ = [
    "BrowserFactory",
    "classify_navigation_error",
    "context_options",
    "launch_options",
    "run_session",
    "summarize_error",
]
