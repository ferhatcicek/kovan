"""Dışa aktarılan gizli değerlerin parola ile şifrelenmesi: scrypt (KDF) + AES-256-GCM."""
from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
MIN_PASSPHRASE = 8
_AAD = b"kovan-secrets-v1"


class DecryptError(ValueError):
    pass


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(passphrase.encode("utf-8"))


def encrypt_json(obj, passphrase: str) -> dict:
    if len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"Şifreleme parolası en az {MIN_PASSPHRASE} karakter olmalı")
    salt, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(passphrase, salt)).encrypt(nonce, json.dumps(obj).encode("utf-8"), _AAD)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return {"alg": "scrypt+aes-256-gcm", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt": b64(salt), "nonce": b64(nonce), "ciphertext": b64(ct)}


def decrypt_json(blob: dict, passphrase: str):
    try:
        if blob.get("alg") != "scrypt+aes-256-gcm":
            raise DecryptError("Desteklenmeyen şifreleme biçimi")
        salt, nonce, ct = (base64.b64decode(blob[k]) for k in ("salt", "nonce", "ciphertext"))
        if blob.get("n") != SCRYPT_N or len(salt) != 16 or len(nonce) != 12:
            raise DecryptError("Geçersiz şifreleme parametreleri")
        return json.loads(AESGCM(_key(passphrase, salt)).decrypt(nonce, ct, _AAD))
    except InvalidTag as e:
        raise DecryptError("Parola hatalı veya dosya değiştirilmiş") from e
    except (KeyError, ValueError, TypeError) as e:
        if isinstance(e, DecryptError):
            raise
        raise DecryptError("Şifreli blok okunamadı") from e
