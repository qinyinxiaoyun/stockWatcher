"""Enterprise WeChat callback signing and AES message encryption.

The wire format is the one documented by WeCom: AES-256-CBC with the first
16 bytes of the key as IV, 32-byte PKCS#7 padding, and a plaintext envelope of
``random(16) || network-order length || message || receive_id``.

Only protocol-safe error categories are exposed.  Key material, ciphertext,
and plaintext are deliberately excluded from exception messages.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import struct
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


MAX_XML_BYTES = 256 * 1024
MAX_PLAINTEXT_BYTES = 256 * 1024


class WeComCryptoError(ValueError):
    """Base class for safe-to-report callback protocol failures."""

    reason = "invalid_wecom_message"

    def __init__(self, reason: str | None = None) -> None:
        super().__init__(reason or self.reason)
        self.reason = reason or self.reason


class InvalidKeyError(WeComCryptoError):
    reason = "invalid_encoding_aes_key"


class SignatureError(WeComCryptoError):
    reason = "invalid_signature"


class DecryptionError(WeComCryptoError):
    reason = "decrypt_failed"


class ReceiveIdError(WeComCryptoError):
    reason = "receive_id_mismatch"


class MessageFormatError(WeComCryptoError):
    reason = "invalid_message_format"


def message_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    """Return the lowercase SHA-1 signature required by the WeCom protocol."""

    values = (token, str(timestamp), str(nonce), encrypted)
    if not all(isinstance(value, str) for value in values):
        raise TypeError("signature inputs must be strings")
    joined = "".join(sorted(values)).encode("utf-8")
    return hashlib.sha1(joined, usedforsecurity=False).hexdigest()


def _safe_xml_root(xml: str | bytes) -> ET.Element:
    if isinstance(xml, str):
        try:
            raw = xml.encode("utf-8")
        except UnicodeError as exc:
            raise MessageFormatError() from exc
    elif isinstance(xml, bytes):
        raw = xml
    else:
        raise MessageFormatError()
    if not raw or len(raw) > MAX_XML_BYTES:
        raise MessageFormatError()
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise MessageFormatError()
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, UnicodeError, ValueError) as exc:
        raise MessageFormatError() from exc
    if root.tag != "xml":
        raise MessageFormatError()
    return root


def extract_encrypted(xml: str | bytes) -> str:
    """Extract the sole top-level ``Encrypt`` value from a callback body."""

    root = _safe_xml_root(xml)
    elements = root.findall("Encrypt")
    if len(elements) != 1 or not elements[0].text:
        raise MessageFormatError()
    encrypted = elements[0].text.strip()
    if not encrypted or len(encrypted) > MAX_XML_BYTES:
        raise MessageFormatError()
    return encrypted


@dataclass(frozen=True)
class EncryptedReply:
    """Components of an encrypted passive reply."""

    encrypted: str
    signature: str
    timestamp: str
    nonce: str

    def to_xml(self) -> bytes:
        root = ET.Element("xml")
        ET.SubElement(root, "Encrypt").text = self.encrypted
        ET.SubElement(root, "MsgSignature").text = self.signature
        ET.SubElement(root, "TimeStamp").text = self.timestamp
        ET.SubElement(root, "Nonce").text = self.nonce
        return ET.tostring(root, encoding="utf-8", short_empty_elements=False)


class WeComCrypto:
    """Verify and decrypt callbacks for one WeCom application."""

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str) -> None:
        if not isinstance(token, str) or not token:
            raise ValueError("callback token is required")
        if not isinstance(receive_id, str) or not receive_id:
            raise ValueError("receive_id is required")
        if not isinstance(encoding_aes_key, str) or len(encoding_aes_key) != 43:
            raise InvalidKeyError()
        try:
            key = base64.b64decode(encoding_aes_key + "=", validate=True)
        except (ValueError, TypeError) as exc:
            raise InvalidKeyError() from exc
        if len(key) != 32:
            raise InvalidKeyError()
        self._token = token
        self._key = key
        self._receive_id = receive_id.encode("utf-8")

    def signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        return message_signature(self._token, timestamp, nonce, encrypted)

    def verify_signature(
        self,
        signature: str,
        timestamp: str,
        nonce: str,
        encrypted: str,
    ) -> None:
        if (
            not isinstance(signature, str)
            or re.fullmatch(r"[0-9A-Fa-f]{40}", signature) is None
            or not hmac.compare_digest(
                self.signature(timestamp, nonce, encrypted), signature.lower()
            )
        ):
            raise SignatureError()

    def decrypt(self, encrypted: str) -> str:
        if not isinstance(encrypted, str) or not encrypted:
            raise DecryptionError()
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
            if not ciphertext or len(ciphertext) % 16:
                raise ValueError
            decryptor = Cipher(
                algorithms.AES(self._key), modes.CBC(self._key[:16])
            ).decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
            unpadder = padding.PKCS7(256).unpadder()
            clear = unpadder.update(padded) + unpadder.finalize()
        except Exception as exc:
            # cryptography deliberately exposes several backend exception
            # classes.  Collapse them so no ciphertext or key leaks upward.
            raise DecryptionError() from exc

        if len(clear) < 20:
            raise MessageFormatError()
        message_length = struct.unpack(">I", clear[16:20])[0]
        end = 20 + message_length
        if message_length > MAX_PLAINTEXT_BYTES or end > len(clear):
            raise MessageFormatError()
        message = clear[20:end]
        received_id = clear[end:]
        if not hmac.compare_digest(received_id, self._receive_id):
            raise ReceiveIdError()
        try:
            return message.decode("utf-8")
        except UnicodeError as exc:
            raise MessageFormatError() from exc

    def verify_and_decrypt(
        self,
        encrypted: str,
        signature: str,
        timestamp: str,
        nonce: str,
    ) -> str:
        self.verify_signature(signature, timestamp, nonce, encrypted)
        return self.decrypt(encrypted)

    def verify_url(
        self,
        signature: str,
        timestamp: str,
        nonce: str,
        echo: str,
    ) -> str:
        """Validate the callback URL handshake and return its plaintext echo."""

        return self.verify_and_decrypt(echo, signature, timestamp, nonce)

    def decrypt_message(
        self,
        xml: str | bytes,
        signature: str,
        timestamp: str,
        nonce: str,
    ) -> str:
        encrypted = extract_encrypted(xml)
        return self.verify_and_decrypt(encrypted, signature, timestamp, nonce)

    def encrypt(self, plaintext: str, *, random_bytes: bytes | None = None) -> str:
        """Encrypt plaintext; ``random_bytes`` exists for deterministic tests."""

        if not isinstance(plaintext, str):
            raise TypeError("plaintext must be a string")
        try:
            message = plaintext.encode("utf-8")
        except UnicodeError as exc:
            raise MessageFormatError() from exc
        if len(message) > MAX_PLAINTEXT_BYTES:
            raise MessageFormatError("message_too_large")
        random_prefix = os.urandom(16) if random_bytes is None else random_bytes
        if not isinstance(random_prefix, bytes) or len(random_prefix) != 16:
            raise ValueError("random_bytes must contain exactly 16 bytes")
        clear = (
            random_prefix
            + struct.pack(">I", len(message))
            + message
            + self._receive_id
        )
        padder = padding.PKCS7(256).padder()
        padded = padder.update(clear) + padder.finalize()
        encryptor = Cipher(
            algorithms.AES(self._key), modes.CBC(self._key[:16])
        ).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(ciphertext).decode("ascii")

    def encrypt_reply(
        self,
        plaintext: str,
        nonce: str,
        timestamp: str | None = None,
        *,
        random_bytes: bytes | None = None,
    ) -> EncryptedReply:
        timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
        nonce = str(nonce)
        encrypted = self.encrypt(plaintext, random_bytes=random_bytes)
        return EncryptedReply(
            encrypted=encrypted,
            signature=self.signature(timestamp, nonce, encrypted),
            timestamp=timestamp,
            nonce=nonce,
        )


__all__ = [
    "DecryptionError",
    "EncryptedReply",
    "InvalidKeyError",
    "MessageFormatError",
    "ReceiveIdError",
    "SignatureError",
    "WeComCrypto",
    "WeComCryptoError",
    "extract_encrypted",
    "message_signature",
]
