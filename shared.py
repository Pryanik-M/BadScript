"""
shared.py — Общие утилиты для клиента и сервера.
"""
import base64
import hashlib
import json
import os
import platform
import subprocess
import uuid
from cryptography.fernet import Fernet

CHUNK_SIZE = 1024 * 512

IS_WINDOWS = platform.system() == "Windows"


def _subproc_flags():
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0) if IS_WINDOWS else 0


def _machine_fingerprint() -> str:
    fp_parts = [platform.node(), platform.machine()]
    flags = _subproc_flags()
    try:
        if IS_WINDOWS:
            result = subprocess.run(
                ['wmic', 'csproduct', 'get', 'UUID'],
                capture_output=True, text=True, timeout=5,
                creationflags=flags,
            )
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line and line != "UUID":
                    fp_parts.append(line)
                    break
        else:
            for path in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
                if os.path.isfile(path):
                    with open(path, 'r') as f:
                        fp_parts.append(f.read().strip())
                    break
    except Exception:
        pass
    try:
        if IS_WINDOWS:
            result = subprocess.run(
                ['wmic', 'diskdrive', 'get', 'serialnumber'],
                capture_output=True, text=True, timeout=5,
                creationflags=flags,
            )
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line and line != "SerialNumber":
                    fp_parts.append(line)
                    break
    except Exception:
        pass
    return "|".join(fp_parts)


def derive_key(secret: str) -> bytes:
    fp = _machine_fingerprint()
    combined = secret + ":" + fp
    for _ in range(100000):
        combined = hashlib.sha256(combined.encode()).hexdigest()
    raw = hashlib.sha256(combined.encode()).digest()
    return base64.urlsafe_b64encode(raw[:32])


def derive_key_simple(secret: str) -> bytes:
    raw = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(raw[:32])


def make_fernet(secret: str, use_machine_key: bool = False) -> Fernet:
    if use_machine_key:
        return Fernet(derive_key(secret))
    return Fernet(derive_key_simple(secret))


def encrypt_msg(fernet: Fernet, obj) -> bytes:
    return fernet.encrypt(json.dumps(obj).encode())


def decrypt_msg(fernet: Fernet, data: bytes) -> dict:
    return json.loads(fernet.decrypt(data))


def new_msg_id() -> str:
    return uuid.uuid4().hex[:12]
