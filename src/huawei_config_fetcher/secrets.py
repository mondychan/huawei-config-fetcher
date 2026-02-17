from __future__ import annotations

import base64
import os
import secrets
from typing import Optional

import keyring
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


KDF_N = 2**14
KDF_R = 8
KDF_P = 1
KDF_LEN = 32


def generate_master_password() -> str:
    return secrets.token_urlsafe(18)


def generate_data_key() -> bytes:
    return secrets.token_bytes(32)


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def derive_key(password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=KDF_LEN, n=KDF_N, r=KDF_R, p=KDF_P)
    return kdf.derive(password.encode("utf-8"))


def wrap_data_key(master_password: str, data_key: bytes) -> dict:
    salt = os.urandom(16)
    key = derive_key(master_password, salt)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    wrapped = aesgcm.encrypt(nonce, data_key, None)
    return {
        "kdf": "scrypt",
        "salt_b64": _b64encode(salt),
        "wrap_nonce_b64": _b64encode(nonce),
        "wrapped_key_b64": _b64encode(wrapped),
    }


def unwrap_data_key(master_password: str, salt_b64: str, wrap_nonce_b64: str, wrapped_key_b64: str) -> bytes:
    salt = _b64decode(salt_b64)
    nonce = _b64decode(wrap_nonce_b64)
    wrapped = _b64decode(wrapped_key_b64)
    key = derive_key(master_password, salt)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, wrapped, None)


def data_key_from_env(salt_b64: str, wrap_nonce_b64: str, wrapped_key_b64: str) -> Optional[bytes]:
    data_key_b64 = os.getenv("HCF_DATA_KEY_B64")
    if data_key_b64:
        return _b64decode(data_key_b64.strip())

    master_password = os.getenv("HCF_MASTER_PASSWORD")
    if master_password:
        return unwrap_data_key(master_password, salt_b64, wrap_nonce_b64, wrapped_key_b64)

    return None


def encrypt_secret(data_key: bytes, plaintext: str) -> str:
    aesgcm = AESGCM(data_key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return _b64encode(nonce + ciphertext)


def decrypt_secret(data_key: bytes, blob_b64: str) -> str:
    raw = _b64decode(blob_b64)
    nonce = raw[:12]
    ciphertext = raw[12:]
    aesgcm = AESGCM(data_key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")


def get_keyring_data_key(service: str, user: str) -> Optional[bytes]:
    try:
        value = keyring.get_password(service, user)
    except Exception:
        return None
    if not value:
        return None
    try:
        return _b64decode(value)
    except Exception:
        return None


def set_keyring_data_key(service: str, user: str, data_key: bytes) -> bool:
    try:
        keyring.set_password(service, user, _b64encode(data_key))
        return True
    except Exception:
        return False


def clear_keyring_data_key(service: str, user: str) -> None:
    try:
        keyring.delete_password(service, user)
    except Exception:
        pass
