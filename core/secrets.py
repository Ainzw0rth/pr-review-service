from cryptography.fernet import Fernet
from django.conf import settings


def cipher():
    if not settings.CREDENTIAL_KEY:
        raise RuntimeError("CREDENTIAL_KEY must be configured for credentials")
    return Fernet(settings.CREDENTIAL_KEY.encode())


def encrypt(value):
    return cipher().encrypt(value.encode()).decode()


def decrypt(value):
    return cipher().decrypt(value.encode()).decode()
