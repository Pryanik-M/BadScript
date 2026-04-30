"""
shared.py — Общие утилиты для клиента и сервера.
"""
import base64
import hashlib
import json
import uuid
from cryptography.fernet import Fernet

CHUNK_SIZE = 1024 * 512


def derive_key(secret: str) -> bytes:
    raw = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(raw[:32])


def make_fernet(secret: str) -> Fernet:
    return Fernet(derive_key(secret))


def encrypt_msg(fernet: Fernet, obj) -> bytes:
    return fernet.encrypt(json.dumps(obj).encode())


def decrypt_msg(fernet: Fernet, data: bytes) -> dict:
    return json.loads(fernet.decrypt(data))


def new_msg_id() -> str:
    return uuid.uuid4().hex[:12]
