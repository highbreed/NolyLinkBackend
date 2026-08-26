"""SQLAlchemy TypeDecorator for encrypting a column at rest (currently: branches.cmc_key)."""

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.core.security import get_encryption_backend


class EncryptedString(TypeDecorator[str]):
    """Transparently encrypts on write, decrypts on read, via the pluggable backend in
    app.core.security.get_encryption_backend(). The underlying column stays a plain
    unbounded VARCHAR — only the Python-side value differs — so this never requires an
    Alembic migration of its own.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return get_encryption_backend().encrypt(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return get_encryption_backend().decrypt(value)
