"""Validation helpers shared by profile and launcher services."""

from __future__ import annotations

import re

from .errors import InvalidNameError

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_name(value: str, label: str) -> str:
    """Guard a profile or command name against traversal and shell syntax."""
    if not value or not _NAME_RE.match(value):
        raise InvalidNameError(
            f"invalid {label} {value!r}. Allowed: letters, numbers, dot, "
            "underscore, dash."
        )
    return value
