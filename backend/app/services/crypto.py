import base64
import hashlib
import os
import logging
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# 固定 SALT 用来将任意长度的环境变量 CONFIG_ENCRYPTION_KEY 派生成 Fernet 所需的 32 字节密钥
_CRYPTO_SALT = b"jm-sp-bot-fixed-salt-16bytes"
_cipher: Fernet | None = None

def get_encryption_cipher() -> Fernet:
    global _cipher
    if _cipher is not None:
        return _cipher
    
    raw_key = os.getenv("CONFIG_ENCRYPTION_KEY")
    if not raw_key:
        logger.warning(
            "⚠️ CONFIG_ENCRYPTION_KEY environment variable is not set. "
            "Using default fallback key. This is INSECURE for production environments!"
        )
        raw_key = "jm-sp-bot-default-fallback-insecure-key-change-me"
        
    # 用 PBKDF2 算法加上固定的 Salt 派生 32 字节数据，再做 url-safe base64 编码以构造 Fernet
    derived = hashlib.pbkdf2_hmac("sha256", raw_key.encode("utf-8"), _CRYPTO_SALT, 100000)
    fernet_key = base64.urlsafe_b64encode(derived)
    _cipher = Fernet(fernet_key)
    return _cipher

def encrypt_value(val: str) -> str:
    if not val:
        return val
    cipher = get_encryption_cipher()
    encrypted_bytes = cipher.encrypt(val.encode("utf-8"))
    return "enc:" + encrypted_bytes.decode("utf-8")

def decrypt_value(val: str) -> str:
    if not val or not val.startswith("enc:"):
        return val
    cipher = get_encryption_cipher()
    try:
        cipher_text = val.removeprefix("enc:").encode("utf-8")
        decrypted_bytes = cipher.decrypt(cipher_text)
        return decrypted_bytes.decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to decrypt configuration value: {e}")
        # 如果解密失败（比如密钥不对），安全回退返回原密文值，避免抛出未捕获异常导致系统崩溃
        return val
