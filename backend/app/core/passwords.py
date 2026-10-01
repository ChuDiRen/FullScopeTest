"""
密码哈希（纯 hashlib，零第三方依赖）

哈希格式: scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>
"""

import hashlib
import hmac
import os

_N = 32768
_R = 8
_P = 1
_MAXMEM = 64 * 1024 * 1024  # OpenSSL 默认 maxmem 不足以跑 n=32768 的 scrypt


def generate_password_hash(password: str) -> str:
    """生成 scrypt 哈希"""
    if isinstance(password, str):
        password = password.encode("utf-8")
    salt = os.urandom(16)
    digest = hashlib.scrypt(password, salt=salt, n=_N, r=_R, p=_P, dklen=48, maxmem=_MAXMEM)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def check_password_hash(pwhash: str, password: str) -> bool:
    """校验口令（scrypt$n$r$p$salt_hex$hash_hex 格式）"""
    if not pwhash or not password:
        return False
    try:
        prefix = f"scrypt${_N}${_R}${_P}$"
        if not pwhash.startswith(prefix):
            return False
        _, n, r, p, salt_hex, hash_hex = pwhash.split("$")
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
            maxmem=_MAXMEM,
        )
        return hmac.compare_digest(digest, bytes.fromhex(hash_hex))
    except (ValueError, TypeError):
        return False
