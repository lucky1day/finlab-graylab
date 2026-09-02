from __future__ import annotations

import hashlib
import re
import secrets

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError


SESSION_TOKEN_BYTES = 32
PASSWORD_MIN_LENGTH = 6
PASSWORD_MAX_LENGTH = 128
USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}\Z", re.ASCII)

_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash(
    "BondFactorLab-Dummy-Password-Only-2026"
)


class PasswordPolicyError(ValueError):
    """密码不满足固定产品规则。"""


class UsernamePolicyError(ValueError):
    """用户名不满足固定标准化规则。"""


def normalize_username(value: str) -> str:
    """将用户名标准化为小写，并拒绝裁剪或非合同字符。"""
    normalized = value.lower()
    if USERNAME_PATTERN.fullmatch(normalized) is None:
        raise UsernamePolicyError("invalid_username")
    return normalized


def validate_password(value: str) -> None:
    """验证已确认的 6–128 位大小写字母加数字规则。"""
    if not PASSWORD_MIN_LENGTH <= len(value) <= PASSWORD_MAX_LENGTH:
        raise PasswordPolicyError("invalid_password")
    if not any("A" <= char <= "Z" for char in value):
        raise PasswordPolicyError("invalid_password")
    if not any("a" <= char <= "z" for char in value):
        raise PasswordPolicyError("invalid_password")
    if not any("0" <= char <= "9" for char in value):
        raise PasswordPolicyError("invalid_password")


def hash_password(value: str) -> str:
    """校验后使用固定 Argon2id 参数生成密码哈希。"""
    validate_password(value)
    return _PASSWORD_HASHER.hash(value)


def verify_password(password_hash: str, candidate: str) -> bool:
    """恒定调用 Argon2 verifier；非法哈希按不匹配处理。"""
    try:
        return bool(_PASSWORD_HASHER.verify(password_hash, candidate))
    except (InvalidHashError, VerificationError, ValueError):
        return False


def verify_dummy_password(candidate: str) -> None:
    """为不可区分登录失败执行与真实账号同参数的 Argon2 校验。"""
    verify_password(_DUMMY_PASSWORD_HASH, candidate)


def new_session_token() -> str:
    """生成 256 位不可预测的 URL-safe opaque token。"""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def digest_session_token(token: str) -> bytes:
    """数据库仅保存会话 token 的 SHA-256 摘要。"""
    return hashlib.sha256(token.encode("ascii", errors="strict")).digest()
