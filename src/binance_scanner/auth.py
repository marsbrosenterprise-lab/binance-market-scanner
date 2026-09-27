from __future__ import annotations

from dataclasses import dataclass
from hmac import compare_digest


@dataclass(frozen=True, slots=True)
class AuthenticatedOperator:
    operator_id: str


def authenticate_operator(
    provided_token: str | None, configured_token: str | None, operator_id: str
) -> AuthenticatedOperator:
    if not configured_token:
        raise RuntimeError("approval token is not configured")
    if provided_token is None or not compare_digest(provided_token, configured_token):
        raise PermissionError("invalid approval token")
    return AuthenticatedOperator(operator_id=operator_id)
