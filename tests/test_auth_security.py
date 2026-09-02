from __future__ import annotations

import re

import pytest

from backend.auth.security import (
    PasswordPolicyError,
    UsernamePolicyError,
    digest_session_token,
    hash_password,
    new_session_token,
    normalize_username,
    validate_password,
    verify_password,
)
from backend.auth.service import AuthError, AuthService


@pytest.mark.parametrize(
    "password",
    (
        "Aa1234",
        "密码 Aa1",
        " spaced Aa1 ",
        "Z9z" + "x" * 125,
    ),
)
def test_password_policy_accepts_confirmed_product_contract(password: str) -> None:
    validate_password(password)
    encoded = hash_password(password)
    assert encoded.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert verify_password(encoded, password)
    assert not verify_password(encoded, password + "x")


@pytest.mark.parametrize(
    "password",
    (
        "Aa123",
        "a12345",
        "A12345",
        "Aaaaaa",
        "Aa1" + "x" * 126,
    ),
)
def test_password_policy_rejects_invalid_values(password: str) -> None:
    with pytest.raises(PasswordPolicyError, match="invalid_password"):
        validate_password(password)


def test_password_policy_does_not_trim_whitespace() -> None:
    raw = " Aa1 "
    with pytest.raises(PasswordPolicyError):
        validate_password(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    (("Admin", "admin"), ("user.name", "user.name"), ("a_b-1", "a_b-1")),
)
def test_username_is_ascii_binary_normalized(raw: str, expected: str) -> None:
    assert normalize_username(raw) == expected


@pytest.mark.parametrize("raw", ("ab", " admin", "用户1", "a/b", "a" * 33))
def test_username_rejects_non_contract_values(raw: str) -> None:
    with pytest.raises(UsernamePolicyError, match="invalid_username"):
        normalize_username(raw)


def test_session_token_is_256_bit_and_only_digest_is_persistable() -> None:
    token = new_session_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    digest = digest_session_token(token)
    assert isinstance(digest, bytes)
    assert len(digest) == 32
    assert token.encode() not in digest


@pytest.mark.parametrize(
    "operation",
    (
        lambda service: service.change_password(
            "é", "Current123", "Changed456", "request"
        ),
        lambda service: service.list_users("é"),
        lambda service: service.change_username(
            "é", user_id=1, username="valid.user", request_id="request"
        ),
        lambda service: service.change_role(
            "é", user_id=1, role="user", request_id="request"
        ),
        lambda service: service.change_status(
            "é", user_id=1, status="disabled", request_id="request"
        ),
        lambda service: service.create_user(
            "é",
            username="valid.user",
            initial_password="Secret123",
            role="user",
            request_id="request",
        ),
        lambda service: service.reset_password(
            "é", user_id=1, new_password="Secret123", request_id="request"
        ),
    ),
)
def test_malformed_non_ascii_session_token_is_unauthenticated(operation) -> None:
    service = AuthService(object())
    with pytest.raises(AuthError) as caught:
        operation(service)
    assert caught.value.error_code == "not_authenticated"
    assert caught.value.status_code == 401
