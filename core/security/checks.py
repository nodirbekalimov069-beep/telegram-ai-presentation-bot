"""Constant-time shared-secret and admin checks."""

import hmac


class AuthorizationError(PermissionError):
    pass


def verify_shared_secret(received: str, expected: str) -> None:
    if not expected or not hmac.compare_digest(received, expected):
        raise AuthorizationError("invalid shared secret")


def require_admin(telegram_user_id: int, admin_ids: frozenset[int]) -> None:
    if telegram_user_id not in admin_ids:
        raise AuthorizationError("administrator access required")
