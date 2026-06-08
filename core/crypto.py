import base64
import hashlib

from cryptography.fernet import Fernet
from django.conf import settings


def get_fernet():
    key = settings.SECRET_ENCRYPTION_KEY
    if key:
        return Fernet(key.encode())

    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_text(value):
    if value == "":
        return ""
    return get_fernet().encrypt(value.encode()).decode()


def decrypt_text(value):
    if value == "":
        return ""
    return get_fernet().decrypt(value.encode()).decode()
