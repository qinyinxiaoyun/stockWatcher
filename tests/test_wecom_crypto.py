"""Offline tests for the Enterprise WeChat callback cryptography."""

from __future__ import annotations

import base64

import pytest

from stock_watch.wecom_crypto import (
    DecryptionError,
    InvalidKeyError,
    MessageFormatError,
    ReceiveIdError,
    SignatureError,
    WeComCrypto,
    extract_encrypted,
    message_signature,
)


# Fixed external vector from the Enterprise WeChat "加解密方案说明" example:
# https://developer.work.weixin.qq.com/document/path/90238
# The exact ciphertext/signature/plaintext is also carried by the Enterprise
# WeChat team's official example library at:
# https://github.com/sbzhu/weworkapi_php/blob/master/callback/Sample.php
OFFICIAL_TOKEN = "QDG6eK"
OFFICIAL_AES_KEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
OFFICIAL_CORP_ID = "wx5823bf96d3bd56c7"
OFFICIAL_TIMESTAMP = "1409659813"
OFFICIAL_NONCE = "1372623149"
OFFICIAL_SIGNATURE = "477715d11cdb4164915debcba66cb864d751f3e6"
OFFICIAL_ENCRYPTED = (
    "RypEvHKD8QQKFhvQ6QleEB4J58tiPdvo+rtK1I9qca6aM/wvqnLSV5zEPeusUiX5"
    "L5X/0lWfrf0QADHHhGd3QczcdCUpj911L3vg3W/sYYvuJTs3TUUkSUXxaccAS0qhx"
    "chrRYt66wiSpGLYL42aM6A8dTT+6k4aSknmPj48kzJs8qLjvd4Xgpue06DOdnLxAUH"
    "zM6+kDZ+HMZfJYuR+LtwGc2hgf5gsijff0ekUNXZiqATP7PF5mZxZ3Izoun1s4zG4L"
    "UMnvw2r+KqCKIw+3IQH03v+BCA9nMELNqbSf6tiWSrXJB3LAVGUcallcrw8V2t9EL4"
    "EhzJWrQUax5wLVMNS0+rUPA3k22Ncx4XXZS9o0MBH27Bo6BpNelZpS+/uh9KsNlY6b"
    "HCmJU9p8g7m3fVKn28H3KDYA5Pl/T8Z1ptDAVe0lXdQ2YoyyH2uyPIGHBZZIs2pDBS8"
    "R07+qN+E7Q=="
)


def official_crypto() -> WeComCrypto:
    return WeComCrypto(OFFICIAL_TOKEN, OFFICIAL_AES_KEY, OFFICIAL_CORP_ID)


def test_official_external_vector_signature_and_plaintext() -> None:
    crypto = official_crypto()
    plaintext = crypto.verify_and_decrypt(
        OFFICIAL_ENCRYPTED,
        OFFICIAL_SIGNATURE,
        OFFICIAL_TIMESTAMP,
        OFFICIAL_NONCE,
    )
    assert crypto.signature(
        OFFICIAL_TIMESTAMP, OFFICIAL_NONCE, OFFICIAL_ENCRYPTED
    ) == OFFICIAL_SIGNATURE
    assert "<Content><![CDATA[hello]]></Content>" in plaintext
    assert "<MsgId>4561255354251345929</MsgId>" in plaintext
    assert "<AgentID>218</AgentID>" in plaintext


def test_unicode_round_trip_and_encrypted_reply_shape() -> None:
    crypto = official_crypto()
    plaintext = "<xml><Content>兆易创新📈</Content></xml>"
    encrypted = crypto.encrypt(plaintext, random_bytes=b"0123456789abcdef")
    assert crypto.decrypt(encrypted) == plaintext

    reply = crypto.encrypt_reply(
        plaintext,
        "nonce",
        "1700000000",
        random_bytes=b"fedcba9876543210",
    )
    assert reply.signature == message_signature(
        OFFICIAL_TOKEN, "1700000000", "nonce", reply.encrypted
    )
    assert extract_encrypted(reply.to_xml()) == reply.encrypted


def test_wrong_signature_is_rejected_before_decryption() -> None:
    for signature in ("0" * 40, "签名" * 20, "f" * 39):
        with pytest.raises(SignatureError):
            official_crypto().verify_and_decrypt(
                OFFICIAL_ENCRYPTED,
                signature,
                OFFICIAL_TIMESTAMP,
                OFFICIAL_NONCE,
            )


def test_wrong_receive_id_is_rejected() -> None:
    crypto = WeComCrypto(OFFICIAL_TOKEN, OFFICIAL_AES_KEY, "another-corp")
    with pytest.raises(ReceiveIdError):
        crypto.decrypt(OFFICIAL_ENCRYPTED)


def test_invalid_key_and_ciphertext_have_sanitized_errors() -> None:
    with pytest.raises(InvalidKeyError) as caught:
        WeComCrypto("token", "short", "corp")
    assert OFFICIAL_AES_KEY not in str(caught.value)

    with pytest.raises(DecryptionError) as caught:
        official_crypto().decrypt(base64.b64encode(b"x" * 16).decode())
    assert "eHh4" not in str(caught.value)


@pytest.mark.parametrize(
    "xml",
    [
        b"",
        b"<not-xml><Encrypt>x</Encrypt></not-xml>",
        b"<xml></xml>",
        b"<xml><Encrypt>a</Encrypt><Encrypt>b</Encrypt></xml>",
        b'<!DOCTYPE xml [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><xml>&xxe;</xml>',
    ],
)
def test_malformed_or_unsafe_envelope_is_rejected(xml: bytes) -> None:
    with pytest.raises(MessageFormatError):
        extract_encrypted(xml)


def test_invalid_random_prefix_length_is_rejected() -> None:
    with pytest.raises(ValueError, match="exactly 16"):
        official_crypto().encrypt("hello", random_bytes=b"too short")
