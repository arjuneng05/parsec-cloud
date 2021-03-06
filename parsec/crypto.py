from os import urandom
import struct
import base64
import hashlib

from cryptography.fernet import Fernet
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.backends.openssl import backend as openssl
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.ciphers.modes import GCM
from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.exceptions import InvalidSignature, InvalidTag


def hash_id_password(id, password):
    raw_hash = hashlib.sha256((id + ':' + password).encode('utf-8')).digest()
    return base64.urlsafe_b64encode(raw_hash).decode('utf-8')


def load_private_key(raw_key, password=None):
    # Only support RSA so far
    return RSAPrivateKey(raw_key, password=password)


def load_public_key(raw_key):
    return RSAPublicKey(raw_key)


def load_sym_key(raw_key: bytes):
    return AESKey(raw_key)


def generate_sym_key():
    raw_key = urandom(32)  # 256bits long key
    return AESKey(raw_key)


def generate_asym_key(size):
    assert size > 1023
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=size,
        backend=default_backend()
    )
    return RSAPrivateKey(private_key)


class BaseSymKey:

    def __init__(self, key: bytes):
        raise NotImplementedError()

    def encrypt(self, cleartext: bytes):
        raise NotImplementedError()

    def decrypt(self, ciphertext: bytes):
        raise NotImplementedError()

    @property
    def key(self):
        raise NotImplementedError()


class BasePrivateAsymKey:
    def __init__(self, key: bytes):
        raise NotImplementedError()

    def sign(self, message):
        raise NotImplementedError()

    def decrypt(self, message):
        raise NotImplementedError()

    @property
    def pub_key(self):
        raise NotImplementedError()

    def export(self, password: str):
        raise NotImplementedError()


class BasePublicAsymKey:
    def __init__(self, key: bytes):
        raise NotImplementedError()

    def verify(self, signature, message):
        raise NotImplementedError()

    def encrypt(self, message):
        raise NotImplementedError()

    def export(self):
        raise NotImplementedError()


class RSAPublicKey(BasePublicAsymKey):
    def __init__(self, key: bytes):
        if isinstance(key, bytes):
            public_key = serialization.load_pem_public_key(key, backend=default_backend())
            self._hazmat_public_key = public_key
        else:
            self._hazmat_public_key = key
        if self._hazmat_public_key.key_size < 1023:
            raise RuntimeError('Minimal key size is 1024bits')

    def verify(self, signature: bytes, message: bytes):
        return self._hazmat_public_key.verify(
            signature,
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )

    def encrypt(self, message: bytes):
        symkey = generate_sym_key()
        ciphertext = symkey.encrypt(message)
        ciphersymkey = self._hazmat_public_key.encrypt(
            symkey.key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )
        return struct.pack(">I", len(ciphersymkey)) + ciphersymkey + ciphertext

    def export(self):
        return self._hazmat_public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )


class RSAPrivateKey(BasePrivateAsymKey):
    def __init__(self, key: bytes, password: str=None):
        self._pub_key = None
        if isinstance(key, bytes):
            private_key = serialization.load_pem_private_key(
                key,
                password=password.encode('utf-8') if password else None,
                backend=default_backend()
            )
            self._hazmat_private_key = private_key
        else:
            self._hazmat_private_key = key
        if self._hazmat_private_key.key_size < 1023:
            raise RuntimeError('Minimal key size is 1024bits')

    def sign(self, message: bytes):
        return self._hazmat_private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )

    def decrypt(self, ciphertext: bytes):
        lenciphersymkey, = struct.unpack(">I", ciphertext[:4])
        ciphersymkey = ciphertext[4:4 + lenciphersymkey]
        ciphertext = ciphertext[4 + lenciphersymkey:]

        symkey = load_sym_key(self._hazmat_private_key.decrypt(
            ciphersymkey,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        ))
        return symkey.decrypt(ciphertext)

    def export(self, password: str):
        return self._hazmat_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(password.encode('utf-8'))
        )

    @property
    def pub_key(self):
        if not self._pub_key:
            self._pub_key = RSAPublicKey(self._hazmat_private_key.public_key())
        return self._pub_key


class AESKey(BaseSymKey):

    def __init__(self, key: bytes):
        self._hazmat_key = AES(key)

    def encrypt(self, cleartext: bytes):
        # No need for padding as we are using GCM
        # Get a new iv for GCM
        iv = urandom(int(AES.block_size // 8))
        cipher = Cipher(self._hazmat_key, GCM(iv), backend=openssl)
        encryptor = cipher.encryptor()
        enc = encryptor.update(cleartext) + encryptor.finalize()
        return iv + enc + encryptor.tag

    def decrypt(self, ciphertext: bytes):
        iv = ciphertext[:AES.block_size // 8]
        tag = ciphertext[-16:]
        cipher = Cipher(self._hazmat_key, GCM(iv, tag), backend=openssl).decryptor()
        return cipher.update(ciphertext[16:-16]) + cipher.finalize()

    @property
    def key(self):
        return self._hazmat_key.key


def _fernet_from_password(password, salt):
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
        backend=default_backend()
    )
    key = base64.urlsafe_b64encode(kdf.derive(password))
    return Fernet(key)


def encrypt_with_password(password: bytes, salt: bytes, text: bytes) -> bytes:
    f = _fernet_from_password(password, salt)
    return f.encrypt(text)


def decrypt_with_password(password: bytes, salt: bytes, ciphertext: bytes) -> bytes:
    f = _fernet_from_password(password, salt)
    return f.decrypt(ciphertext)


__all__ = (
    'InvalidSignature',
    'InvalidTag',
    'load_private_key',
    'load_public_key',
    'load_sym_key',
    'generate_sym_key',
    'BaseSymKey',
    'BasePrivateAsymKey',
    'BasePublicAsymKey',
    'RSAPublicKey',
    'RSAPrivateKey',
    'AESKey',
    'encrypt_with_password',
    'decrypt_with_password'
)
