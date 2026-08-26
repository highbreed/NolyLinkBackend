"""Inbound client auth (Phase 9) and access to the KRA cmcKey vault (Phase 4)."""

from abc import ABC, abstractmethod
from functools import lru_cache

from cryptography.fernet import Fernet

from app.core.config import get_settings


class CmcKeyEncryptionBackend(ABC):
    """Pluggable encryption backend for cmcKey-at-rest. Swap FernetEncryptionBackend for a
    KMS/Vault-backed implementation in production by changing only get_encryption_backend()
    below — the SQLAlchemy TypeDecorator and every call site stay untouched.
    """

    @abstractmethod
    def encrypt(self, plaintext: str) -> str: ...

    @abstractmethod
    def decrypt(self, ciphertext: str) -> str: ...


class FernetEncryptionBackend(CmcKeyEncryptionBackend):
    """Dev/local backend. Production secrets backend (Vault vs AWS Secrets Manager vs
    Fernet-with-KMS-wrapped-key) is an open decision — see docs/README.md.
    """

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()


@lru_cache
def get_encryption_backend() -> CmcKeyEncryptionBackend:
    return FernetEncryptionBackend(get_settings().cmc_key_encryption_key)
