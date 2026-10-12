from __future__ import annotations

import json
import secrets
from typing import Any, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CREDENTIALS_FORMAT_VERSION: int = 1
CREDENTIALS_ASSOCIATED_DATA: bytes = b"gbrain:source-client-credentials:v1"
CREDENTIALS_SALT: bytes = b"gbrain:credentials-hkdf-salt:v1"


class CryptoError(RuntimeError):
    """Errore applicativo relativo alla crittografia o integrità dei dati."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class DecryptionError(CryptoError):
    """Errore durante la decifratura o autenticazione del ciphertext."""


def derive_credentials_key(secret: str | bytes) -> bytes:
    """Deriva deterministicamente una chiave AES-256 a partire da un segreto master tramite HKDF (SHA-256)."""
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=CREDENTIALS_SALT,
        info=CREDENTIALS_ASSOCIATED_DATA,
    )
    return hkdf.derive(secret_bytes)


def encrypt_payload(
    plaintext: bytes,
    key: bytes,
    *,
    context: bytes = CREDENTIALS_ASSOCIATED_DATA,
) -> bytes:
    """Cifra un payload binario con AES-256-GCM.

    Formato binario:
    - Byte 0: format_version (1)
    - Byte 1..12: Nonce CSPRNG a 96 bit (12 byte)
    - Byte 13..N: Ciphertext con tag di autenticazione GCM in coda
    """
    if len(key) != 32:
        raise CryptoError(
            "invalid_key_length", "La chiave AES-256 deve contenere esattamente 32 byte"
        )
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, context)
    return bytes([CREDENTIALS_FORMAT_VERSION]) + nonce + ciphertext


def decrypt_payload(
    ciphertext: bytes,
    key: bytes,
    *,
    context: bytes = CREDENTIALS_ASSOCIATED_DATA,
) -> bytes:
    """Decifra e autentica un payload binario cifrato con AES-256-GCM."""
    if len(key) != 32:
        raise CryptoError(
            "invalid_key_length", "La chiave AES-256 deve contenere esattamente 32 byte"
        )
    if len(ciphertext) < 29 or ciphertext[0] != CREDENTIALS_FORMAT_VERSION:
        raise DecryptionError(
            "format_version_invalid",
            "Formato delle credenziali cifrate non riconosciuto o non valido",
        )
    nonce = ciphertext[1:13]
    encrypted_data = ciphertext[13:]
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, encrypted_data, context)
    except InvalidTag as exc:
        raise DecryptionError(
            "authentication_tag_mismatch",
            "Integrità o autenticità delle credenziali cifrate compromessa",
        ) from exc


def encrypt_text(text: str, key: bytes, *, context: bytes = CREDENTIALS_ASSOCIATED_DATA) -> bytes:
    """Cifra una stringa UTF-8 con AES-256-GCM."""
    return encrypt_payload(text.encode("utf-8"), key, context=context)


def decrypt_text(
    ciphertext: bytes, key: bytes, *, context: bytes = CREDENTIALS_ASSOCIATED_DATA
) -> str:
    """Decifra un ciphertext in stringa UTF-8."""
    return decrypt_payload(ciphertext, key, context=context).decode("utf-8")


def encrypt_json(
    data: dict[str, Any], key: bytes, *, context: bytes = CREDENTIALS_ASSOCIATED_DATA
) -> bytes:
    """Cifra una struttura dizionario JSON con AES-256-GCM."""
    serialized = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return encrypt_payload(serialized, key, context=context)


def decrypt_json(
    ciphertext: bytes, key: bytes, *, context: bytes = CREDENTIALS_ASSOCIATED_DATA
) -> dict[str, Any]:
    """Decifra un ciphertext in un dizionario JSON."""
    raw = decrypt_payload(ciphertext, key, context=context)
    try:
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise DecryptionError(
                "json_payload_invalid", "Il payload decifrato non è un oggetto JSON valido"
            )
        return cast(dict[str, Any], parsed)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DecryptionError(
            "json_decode_failed", "Impossibile deserializzare il payload decifrato"
        ) from exc
