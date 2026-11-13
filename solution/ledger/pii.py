"""PII protection at rest.

Two different tools for two different needs:

* Matching: events must find their lead by phone number. We store a keyed
  HMAC-SHA256 of the canonical phone. Same phone -> same hash, so an indexed
  equality join works, but the hash can't be reversed or brute-forced without
  the secret (a plain SHA-256 of a 10-digit number could be brute-forced in
  seconds).
* Storage: PII values (name, full phone) are encrypted with Fernet (AES +
  HMAC) so an authorised process can still recover them.

Both keys are derived from one secret in the LEDGER_SECRET environment
variable. In production it would come from a secrets manager.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys

from cryptography.fernet import Fernet

_DEV_SECRET = "dev-only-secret-do-not-use-in-production"


class Pii:
    def __init__(self, secret: str):
        raw = secret.encode()
        self._index_key = hashlib.sha256(b"index:" + raw).digest()
        enc_key = hashlib.sha256(b"encrypt:" + raw).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(enc_key))

    @classmethod
    def from_env(cls) -> "Pii":
        secret = os.environ.get("LEDGER_SECRET")
        if not secret:
            print("warning: LEDGER_SECRET not set, using the dev secret", file=sys.stderr)
            secret = _DEV_SECRET
        return cls(secret)

    def phone_index(self, canonical_phone: str) -> str:
        return hmac.new(self._index_key, canonical_phone.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
