"""Wall-clock helper, isolated so tests can patch it."""

import time


def now_ms() -> int:
    """Current epoch time in milliseconds."""
    return time.time_ns() // 1_000_000
