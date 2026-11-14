"""Recognize explicit notices; enabling deprecated APIs is a separate feature."""

import re

from .core import digest


def evidence(text):
    text = " ".join(str(text).split())
    pattern = r"(?:^(?:deprecated|obsolete)\s*:|\[(?:deprecated|obsolete)\b|\((?:deprecated|obsolete)\b|\b(?:is|has been)\s+deprecated\b)"
    return text if re.search(pattern, text, re.I) else False


def cmake_messages(messages, options):
    """Attach notices from (relative path, source text, message) records."""
    hashes = {}
    for relative, text, message in messages:
        for name, record in options.items():
            if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", message):
                record["deprecated"] = message
                hashes[relative] = digest(text)
    return hashes
