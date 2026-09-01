from __future__ import annotations

import json
import math
from typing import Any


def _reject_non_standard_constant(constant: str) -> Any:
    raise ValueError(f"Non-standard JSON numeric constant is not allowed: {constant}")


def validate_json_values(value: Any) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise ValueError(
                    "JSON strings must contain valid Unicode scalar values."
                )
            continue
        if isinstance(current, float) and not math.isfinite(current):
            raise ValueError("JSON numbers must be finite.")
        if isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
            continue
        if isinstance(current, list):
            pending.extend(current)


def loads_strict_json(text: str) -> Any:
    value = json.loads(text, parse_constant=_reject_non_standard_constant)
    validate_json_values(value)
    return value
