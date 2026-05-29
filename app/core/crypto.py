"""
app/core/crypto.py — optional field-level encryption for secrets at rest.

Sensitive session fields (GCP service-account keys, DB connection strings, LLM
API keys, etc.) are encrypted at the persistence boundary so plaintext secrets
never hit disk or the database. The in-memory session keeps plaintext, so
consumers (BigQuery/source-DB connectors, pipeline) are unchanged.

Activated by setting DM_ENCRYPTION_KEY (a Fernet key, e.g.
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
If unset, the helpers are pass-throughs (backward compatible).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("xref_agent")

_PREFIX = "enc:v1:"

# Field names (lowercased, exact match) whose values must be encrypted at rest.
_SENSITIVE_KEYS = {
    "gcp_creds_json",
    "connection_string",
    "conn_str",
    "password",
    "private_key",
    "api_key",
    "token",
    "secret",
    "client_secret",
}

_fernet = None
_initialised = False


def _get_fernet():
    global _fernet, _initialised
    if _initialised:
        return _fernet
    _initialised = True
    key = os.getenv("DM_ENCRYPTION_KEY", "")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        logger.info("Encryption at rest enabled (DM_ENCRYPTION_KEY set)")
    except Exception as e:  # pragma: no cover - bad key
        logger.error("Invalid DM_ENCRYPTION_KEY — encryption disabled: %s", e)
        _fernet = None
    return _fernet


def enabled() -> bool:
    return _get_fernet() is not None


def encrypt_value(value: Any) -> Any:
    """Encrypt a JSON-serialisable value to a prefixed string. No-op if disabled."""
    f = _get_fernet()
    if f is None:
        return value
    try:
        return _PREFIX + f.encrypt(json.dumps(value).encode()).decode()
    except Exception as e:  # pragma: no cover
        logger.error("Encrypt failed (storing plaintext): %s", e)
        return value


def decrypt_value(value: Any) -> Any:
    """Reverse encrypt_value. Returns the value unchanged if not an enc: string."""
    if not (isinstance(value, str) and value.startswith(_PREFIX)):
        return value
    f = _get_fernet()
    if f is None:
        return value
    try:
        raw = f.decrypt(value[len(_PREFIX):].encode()).decode()
        return json.loads(raw)
    except Exception as e:  # pragma: no cover
        logger.error("Decrypt failed: %s", e)
        return value


def protect_obj(obj: Any) -> Any:
    """Return a deep copy of ``obj`` with sensitive field values encrypted.
    Never mutates the input (important: persistence shares nested refs with
    the live in-memory session)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS and v not in (None, "", {}, []):
                out[k] = encrypt_value(v)
            else:
                out[k] = protect_obj(v)
        return out
    if isinstance(obj, list):
        return [protect_obj(x) for x in obj]
    return obj


def unprotect_obj(obj: Any) -> Any:
    """Reverse protect_obj: decrypt sensitive fields back to plaintext."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS:
                out[k] = decrypt_value(v)
            else:
                out[k] = unprotect_obj(v)
        return out
    if isinstance(obj, list):
        return [unprotect_obj(x) for x in obj]
    return obj
