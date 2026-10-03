"""Shared option and pick normalization from SPEC §7.1."""

import re
import unicodedata


def normalize(text: str) -> str:
    """Apply Unicode NFKC, case folding, trimming, and inner-space collapse."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


normalize_option = normalize
normalize_text = normalize

