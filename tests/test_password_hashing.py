"""Unit tests for password hashing (Fix #4).

Verifies bcrypt hashing, legacy plaintext/sha256 acceptance, and the rehash
signal used to transparently upgrade old credentials on login.
"""
import hashlib

from app.core.auth import (
    _hash_password,
    _verify_password,
    _needs_rehash,
    _looks_hashed,
)


def test_hash_is_bcrypt_and_salted():
    h1 = _hash_password("hunter2")
    h2 = _hash_password("hunter2")
    assert h1.startswith(("$2a$", "$2b$", "$2y$"))
    assert h1 != h2  # unique salt per hash
    assert _looks_hashed(h1)


def test_verify_correct_and_incorrect():
    h = _hash_password("correct horse")
    assert _verify_password("correct horse", h) is True
    assert _verify_password("wrong", h) is False
    assert _verify_password("", h) is False


def test_bcrypt_hash_does_not_need_rehash():
    assert _needs_rehash(_hash_password("x")) is False


def test_legacy_plaintext_is_accepted_but_flagged_for_rehash():
    # Early-MVP records stored the raw password.
    assert _verify_password("xref2026", "xref2026") is True
    assert _needs_rehash("xref2026") is True


def test_legacy_sha256_digest_is_accepted_but_flagged():
    digest = hashlib.sha256("secretpw".encode()).hexdigest()
    assert _verify_password("secretpw", digest) is True
    assert _verify_password("nope", digest) is False
    assert _needs_rehash(digest) is True


def test_empty_stored_credential_never_authenticates():
    # Production seeds an empty admin password when XREF_ADMIN_PASSWORD is unset.
    assert _verify_password("anything", "") is False
    assert _verify_password("", "") is False
