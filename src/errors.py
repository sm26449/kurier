def describe_error(e: BaseException) -> str:
    """Human-readable error text. Some exceptions (e.g. httpx timeouts)
    stringify to an empty string — fall back to the exception type."""
    return str(e) or type(e).__name__
