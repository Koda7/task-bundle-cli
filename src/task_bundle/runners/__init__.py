"""Test runners: parse framework-specific output into structured results.

Importing this package registers the built-in runners with the registry in
``base``. Core ships ``pytest`` and a language-agnostic ``generic`` fallback.
"""

from task_bundle.runners import (  # noqa: F401  (register on import)
    generic,
    gotest,
    jest,
    pytest_runner,
)
from task_bundle.runners.base import (
    RunResult,
    Runner,
    Status,
    TestOutcome,
    available_runners,
    get_runner,
    register_runner,
)

__all__ = [
    "Runner",
    "RunResult",
    "Status",
    "TestOutcome",
    "available_runners",
    "get_runner",
    "register_runner",
]
