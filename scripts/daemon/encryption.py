"""Message encryption/decryption using AES-256-GCM."""

import os
import base64
import stat

from daemon.globals import logger

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False
    logger.warning("[encryption] cryptography package not installed — encryption disabled")

DEFAULT_KEY_PATH = os.path.join(os.path.expanduser("~"), ".claude-daemon", "encryption.key")
IV_SIZE = 12
KEY_BIT_LENGTH = 256


class MessageEncryptor:
    def __init__(self, key_path: str = DEFAULT_KEY_PATH):
        self.key_path = key_path
        self.key = self._load_or_create_key()
        self.aesgcm = AESGCM(self.key)

    def _load_or_create_key(self) -> bytes:
        if os.path.exists(self.key_path):
            raw = open(self.key_path).read().strip()
            if raw:
                logger.info("Encryption key loaded from %s", self.key_path)
                return base64.b64decode(raw)

        key = AESGCM.generate_key(bit_length=KEY_BIT_LENGTH)
        os.makedirs(os.path.dirname(self.key_path), exist_ok=True)
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as f:
            f.write(base64.b64encode(key).decode())
        logger.info("Encryption key generated at %s", self.key_path)
        return key

    def encrypt(self, plaintext: str) -> str:
        iv = os.urandom(IV_SIZE)
        ct = self.aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
        return base64.b64encode(iv + ct).decode("ascii")

    def decrypt(self, encrypted: str) -> str:
        data = base64.b64decode(encrypted)
        iv, ct = data[:IV_SIZE], data[IV_SIZE:]
        return self.aesgcm.decrypt(iv, ct, None).decode("utf-8")

    def get_key_base64(self) -> str:
        return base64.b64encode(self.key).decode("ascii")


_instance: MessageEncryptor | None = None


def get_encryptor(key_path: str = DEFAULT_KEY_PATH) -> MessageEncryptor | None:
    if not _HAS_CRYPTO:
        return None
    global _instance
    if _instance is None:
        _instance = MessageEncryptor(key_path)
    return _instance


def decrypt_message(msg: dict) -> dict:
    if not msg.get("encrypted"):
        return msg
    if not _HAS_CRYPTO:
        return msg
    try:
        enc = get_encryptor()
        if enc:
            msg["text"] = enc.decrypt(msg["text"])
    except Exception as e:
        logger.error("Failed to decrypt message %s: %s", msg.get("id"), e)
    return msg
