"""sha256 hashing for OAuth2 client_secret values — see app/services/client_auth.py's module
docstring for why sha256 rather than bcrypt/passlib (a generated high-entropy secret needs
O(1) lookup-by-hash, not slow salted hashing).

Split into its own leaf module so both client_auth.py (verifying a secret at auth time) and
customer_service.py (generating one at onboarding) can depend on it without a circular import
between those two modules (client_auth -> billing_service -> customer_service already existed
for CustomerNotFoundError, so customer_service can't also import client_auth).
"""

import hashlib


def hash_client_secret(raw_secret: str) -> str:
    return hashlib.sha256(raw_secret.encode()).hexdigest()
