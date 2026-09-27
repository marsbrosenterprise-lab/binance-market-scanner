import pytest

from binance_scanner.auth import authenticate_operator


def test_authenticate_operator_returns_configured_identity() -> None:
    operator = authenticate_operator("secret", "secret", "alice")

    assert operator.operator_id == "alice"


def test_authenticate_operator_rejects_wrong_token() -> None:
    with pytest.raises(PermissionError, match="invalid approval token"):
        authenticate_operator("wrong", "secret", "alice")


def test_authenticate_operator_requires_configuration() -> None:
    with pytest.raises(RuntimeError, match="approval token is not configured"):
        authenticate_operator("secret", None, "alice")
