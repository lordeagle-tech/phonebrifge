"""
One-time-passcode (OTP) extraction from SMS bodies.

A pure-Python helper used by the SMS poller to decide which incoming
message contains a code worth copying to the laptop clipboard. Designed
to be conservative — we'd rather miss an unusually formatted code than
auto-copy a phone number or zip code from an unrelated text.

Strategy (in order, first match wins):
1. Vendor-prefixed forms like ``G-123456`` (Google), ``B-12345`` etc.
2. A keyword (code / OTP / verification / passcode / PIN / TAN / token)
   followed within ~50 chars by a 4–8 digit group, optionally split by
   a single space or dash (``123 456`` → ``123456``).
3. As a last resort on short messages (≤160 chars), the first standalone
   4–8 digit group, skipping obvious 4-digit years (``19xx``/``20xx``).

Phone numbers (long sequences of digits, separated by spaces/dashes/
parentheses/+ signs) are intentionally ignored because the digit groups
inside them never appear as a *standalone* 4–8 digit token.
"""
from __future__ import annotations

import re
from typing import Optional

# Keywords that strongly suggest the surrounding digits are a code.
_KEYWORDS_RE = re.compile(
    r"\b(?:OTP|code|c[oó]digo|passcode|password|pwd|verification|verify|"
    r"verifying|auth(?:enticate|enticator|orization)?|"
    r"login|sign[\s-]?in|PIN|TAN|token|access)\b",
    re.IGNORECASE,
)

# Vendor-prefixed forms: a 1–4 letter tag, dash, 4–8 digits.
# Matches G-123456 (Google), TX-12345 (banks), etc.
_PREFIXED_RE = re.compile(r"\b[A-Z]{1,4}-(\d{4,8})\b")

# A 4–8 digit group not adjacent to other digits.
_GROUP_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")

# A 3+3 digit group with a single space or dash between the halves
# (e.g. "Your code: 123 456"). 6-digit codes are by far the most common
# split form, so we only handle the 3+3 case.
_SPLIT_GROUP_RE = re.compile(r"(?<!\d)(\d{3})[\s\-](\d{3})(?!\d)")


def _looks_like_year(digits: str) -> bool:
    return len(digits) == 4 and (digits.startswith("19") or digits.startswith("20"))


def extract_otp(body: str) -> Optional[str]:
    """Return the most likely OTP in ``body`` or ``None`` if none found."""
    if not body:
        return None
    text = body.strip()

    # 1. Vendor-prefixed form (highest precedence — least ambiguous).
    m = _PREFIXED_RE.search(text)
    if m:
        return m.group(1)

    # 2. Keyword + nearby digit group. Look both BEFORE and AFTER the
    # keyword so we catch "123456 is your verification code" as well as
    # "Your code is 123456".
    for km in _KEYWORDS_RE.finditer(text):
        start = max(0, km.start() - 40)
        end = min(len(text), km.end() + 60)
        window = text[start:end]
        sm = _SPLIT_GROUP_RE.search(window)
        if sm:
            return sm.group(1) + sm.group(2)
        gm = _GROUP_RE.search(window)
        if gm and not _looks_like_year(gm.group(1)):
            return gm.group(1)

    # No conservative-enough fallback — refuse to auto-copy a code we
    # can't tie to a vendor prefix or a recognised keyword. The message
    # still appears in the UI for the user to copy manually.
    return None
