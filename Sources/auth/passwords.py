"""Versioned scrypt password hashes using the Python standard library."""
import hashlib
import hmac
import secrets

PREFIX = 'scrypt-v1'


def derive(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=131072, r=8, p=1,
                          maxmem=256 * 1024 * 1024, dklen=32)


def hash_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('Password harus terdiri dari 12–256 karakter.')
    salt = secrets.token_bytes(16)
    return f'{PREFIX}${salt.hex()}${derive(password, salt).hex()}'


def verify_password(password, encoded):
    try:
        prefix, salt, expected = encoded.split('$')
        if prefix != PREFIX or len(salt) != 32 or len(expected) != 64:
            return False
        actual = derive(password, bytes.fromhex(salt))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError, AttributeError):
        return False
