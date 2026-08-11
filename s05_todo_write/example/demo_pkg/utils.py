"""Utility functions for the demo package."""


def add(a: int, b: int) -> int:
    """Return the sum of two integers.

    Args:
        a: The first addend.
        b: The second addend.

    Returns:
        The sum of ``a`` and ``b``.

    """
    return a + b


def to_uppercase(text: str) -> str:
    """Convert a string to uppercase.

    Args:
        text: The string to convert.

    Returns:
        The input string converted to uppercase.

    """
    return text.upper()
