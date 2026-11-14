"""Phone number normalization and validation (E.164)."""
import re

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone(number: str, default_country_code: str) -> str:
    """Normalize a phone number to E.164 (``+<countrycode><national>``).

    - ``+40712345678``  → kept as-is
    - ``0040712345678`` → ``+40712345678`` (international prefix → +)
    - ``0712345678``    → ``+40712345678`` (national → country code, drop 0)

    Whitespace, dashes, dots and parentheses are stripped. Raises
    ``ValueError`` when the result is not a valid E.164 number, so an
    unusable number is rejected before it reaches the modem.
    """
    s = "".join(ch for ch in str(number or "") if ch.isdigit() or ch == "+")
    if s.startswith("00"):
        s = "+" + s[2:]
    elif s.startswith("0"):
        s = default_country_code + s[1:]
    if not _E164.match(s):
        raise ValueError(f"Invalid phone number: {number!r}")
    return s
