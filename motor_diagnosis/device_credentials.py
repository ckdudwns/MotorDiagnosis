"""Purpose-specific, per-device credentials provisioned outside runtime state."""

import hmac
import json
import os
import re

from .data import ApiError

IDENTIFIER_PATTERN = r"[A-Z0-9][A-Z0-9_.-]{0,62}"
TOKEN_PATTERN = r"[A-Za-z0-9_-]{32,128}"


def supported_identifier(value):
    return (
        isinstance(value, str) and re.fullmatch(IDENTIFIER_PATTERN, value) is not None
    )


def device_for_token(token, device_id, *, environment, error_prefix):
    if not isinstance(token, str) or re.fullmatch(TOKEN_PATTERN, token) is None:
        raise ApiError(401, "AUTH_REQUIRED", "A device-specific token is required.")
    try:

        def unique_pairs(pairs):
            values = {}
            for key, value in pairs:
                if key in values:
                    raise ValueError()
                values[key] = value
            return values

        values = json.loads(
            os.environ.get(environment, "{}"), object_pairs_hook=unique_pairs
        )
        if not isinstance(values, dict):
            raise ValueError()
        seen = set()
        for key, secret in values.items():
            if (
                not supported_identifier(key)
                or not isinstance(secret, str)
                or re.fullmatch(TOKEN_PATTERN, secret) is None
                or secret in seen
            ):
                raise ValueError()
            seen.add(secret)
    except (ValueError, TypeError):
        raise ApiError(
            503,
            error_prefix + "_AUTH_UNAVAILABLE",
            "Device credentials are not correctly provisioned.",
        ) from None
    authenticated = next(
        (
            key
            for key, secret in values.items()
            if hmac.compare_digest(token.encode("ascii"), secret.encode("ascii"))
        ),
        None,
    )
    if authenticated is None:
        raise ApiError(401, "AUTH_REQUIRED", "A device-specific token is required.")
    if authenticated != device_id:
        raise ApiError(
            403,
            error_prefix + "_FORBIDDEN",
            "The credential belongs to another device.",
        )
    return authenticated
