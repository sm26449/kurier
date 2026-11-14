"""GSM-7 transliteration and SMS segment counting.

A single character outside the GSM 03.38 alphabet (Romanian diacritics,
an em dash, typographic quotes) switches the whole SMS to UCS-2: 70
characters per single SMS, 67 per part of a multipart one, instead of
160 / 153. A 320-character alert costs 5 parts in UCS-2 and 3 in GSM-7.
"""
import math

# GSM 03.38 basic character set.
_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# Extension table: each costs two septets (escape + char).
_EXTENSION = set("^{}\\[~]|€\f")

_TRANSLIT = {
    # Romanian (comma-below and legacy cedilla forms)
    "ă": "a", "â": "a", "î": "i", "ș": "s", "ş": "s", "ț": "t", "ţ": "t",
    "Ă": "A", "Â": "A", "Î": "I", "Ș": "S", "Ş": "S", "Ț": "T", "Ţ": "T",
    # Typography
    "—": "-", "–": "-", "‐": "-", "−": "-", "…": "...",
    "„": '"', "“": '"', "”": '"', "«": '"', "»": '"', "‘": "'", "’": "'", "‚": "'",
    " ": " ", " ": " ", " ": " ", "•": "*", "·": "*",
    "°": " deg", "±": "+/-", "×": "x", "≥": ">=", "≤": "<=", "→": "->", "←": "<-",
}

GSM7_SINGLE, GSM7_PART = 160, 153
UCS2_SINGLE, UCS2_PART = 70, 67


def transliterate(text: str) -> str:
    """Replace common non-GSM characters with GSM-7 equivalents.

    Characters without a mapping (e.g. emoji) are kept — the message then
    stays UCS-2, which is still correct, just more expensive.
    """
    return "".join(_TRANSLIT.get(ch, ch) for ch in text)


def is_gsm7(text: str) -> bool:
    return all(ch in _BASIC or ch in _EXTENSION for ch in text)


def segment_count(text: str) -> int:
    """Number of SMS parts the text needs (estimate, as the modem encodes it)."""
    if is_gsm7(text):
        units = sum(2 if ch in _EXTENSION else 1 for ch in text)
        single, part = GSM7_SINGLE, GSM7_PART
    else:
        units = sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)  # UTF-16 code units
        single, part = UCS2_SINGLE, UCS2_PART
    if units <= single:
        return 1
    return math.ceil(units / part)


def segments_used(router_result: object, text: str) -> int:
    """Parts actually billed: RutOS reports ``data.sms_used``; else estimate."""
    if isinstance(router_result, dict):
        data = router_result.get("data")
        if isinstance(data, dict) and isinstance(data.get("sms_used"), int):
            return data["sms_used"]
    return segment_count(text)


def truncate_to_segments(text: str, max_segments: int, suffix: str = "...") -> str:
    """Shorten ``text`` (ending with ``suffix``) so it fits ``max_segments`` parts."""
    if segment_count(text) <= max_segments:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # largest prefix length that still fits
        mid = (lo + hi + 1) // 2
        if segment_count(text[:mid].rstrip() + suffix) <= max_segments:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + suffix
