"""
Portable catalog and password-unlocked secrets vault for Codex Model Tray.

The catalog file is plain JSON so providers and models can be edited next to the
executable. Sensitive data lives in codex_model_tray.secrets.json as one
password-encrypted vault. A local trust cache can unlock that vault on machines
the user has explicitly trusted; copying only the secrets file to another
machine still requires the vault password before that machine can be trusted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import time
from typing import Any


CATALOG_FILENAME = 'codex_model_tray.catalog.json'
SECRETS_FILENAME = 'codex_model_tray.secrets.json'
TRUST_FILENAME = 'codex_model_tray.trust.json'
KEY_FILENAME = 'codex_model_tray.key'

_VAULT_ALGORITHM = 'portable-password-vault-v2'
_TRUST_ALGORITHM = 'portable-machine-trust-v1'
_LEGACY_SECRET_ALGORITHM = 'portable-hmac-stream-v1'
_VAULT_ITERATIONS = 240_000
_TRUST_ITERATIONS = 120_000
_KEY_CONTEXT = b'CodexModelTray portable settings key v1'
_PASSWORD_CONTEXT = b'CodexModelTray password vault key v2'
_TRUST_CONTEXT = b'CodexModelTray trusted machine key v1'

_unlocked_payload: dict[str, Any] | None = None
_unlocked_key: bytes | None = None
_vault_error = ''


class VaultError(Exception):
    """Base class for user-actionable vault errors."""


class VaultLockedError(VaultError):
    """Raised when a secret operation needs an unlocked vault."""


class VaultPasswordError(VaultError):
    """Raised when the supplied vault password cannot decrypt the vault."""


def settings_dir() -> Path:
    override = os.environ.get('CODEX_MODEL_TRAY_SETTINGS_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def catalog_path() -> Path:
    return settings_dir() / CATALOG_FILENAME


def secrets_path() -> Path:
    return settings_dir() / SECRETS_FILENAME


def trust_path() -> Path:
    return settings_dir() / TRUST_FILENAME


def key_path() -> Path:
    return settings_dir() / KEY_FILENAME


def _read_json(file_path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        if file_path.exists():
            with file_path.open('r', encoding='utf-8') as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else dict(default)
    except Exception:
        pass
    return dict(default)


def _write_json(file_path: Path, data: dict[str, Any]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_suffix(file_path.suffix + '.tmp')
    with temp_path.open('w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
    temp_path.replace(file_path)


def _now() -> int:
    return int(time.time())


def read_catalog() -> dict[str, Any]:
    return _read_json(catalog_path(), {
        'schema_version': 1,
        'default_provider_key': '',
        'provider_aliases': {},
        'providers': [],
        'models': [],
        'pinned_models': [],
    })


def write_catalog(data: dict[str, Any]) -> None:
    normalized = dict(data)
    normalized.setdefault('schema_version', 1)
    normalized.setdefault('providers', [])
    normalized.setdefault('models', [])
    normalized.setdefault('provider_aliases', {})
    normalized.setdefault('pinned_models', [])
    normalized.pop('krouter_style', None)
    _write_json(catalog_path(), normalized)


def _base64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _base64_decode(value: str) -> bytes:
    padding = '=' * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode('ascii'))


def _read_secrets_file() -> dict[str, Any]:
    return _read_json(secrets_path(), {})


def _load_local_key_material() -> bytes:
    passphrase = os.environ.get('CODEX_MODEL_TRAY_SECRET')
    if passphrase:
        return passphrase.encode('utf-8')

    file_path = key_path()
    try:
        if file_path.exists():
            text = file_path.read_text(encoding='utf-8').strip()
            if text:
                return _base64_decode(text)
    except Exception:
        pass
    raise VaultLockedError('Legacy key material is unavailable.')


def _derive_master_key(salt: bytes) -> bytes:
    key_material = _load_local_key_material()
    return hashlib.pbkdf2_hmac(
        'sha256',
        key_material + _KEY_CONTEXT,
        salt,
        120_000,
        dklen=32,
    )


def _derive_password_key(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8') + _PASSWORD_CONTEXT,
        salt,
        iterations,
        dklen=32,
    )


def _machine_material() -> bytes:
    parts = [
        sys.platform,
        os.environ.get('COMPUTERNAME', ''),
        os.environ.get('USERDOMAIN', ''),
        os.environ.get('USERNAME', ''),
        os.environ.get('USERPROFILE', ''),
    ]
    if sys.platform == 'win32':
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r'SOFTWARE\Microsoft\Cryptography',
            ) as key:
                machine_guid, _kind = winreg.QueryValueEx(key, 'MachineGuid')
            parts.append(str(machine_guid))
        except Exception:
            pass
    else:
        for candidate in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
            try:
                text = Path(candidate).read_text(encoding='utf-8').strip()
                if text:
                    parts.append(text)
                    break
            except Exception:
                pass

    joined = '\n'.join(part for part in parts if part)
    return joined.encode('utf-8') or b'unknown-machine'


def _machine_hash() -> str:
    return _base64_encode(hashlib.sha256(_machine_material() + _TRUST_CONTEXT).digest())


def _derive_trust_key(salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        'sha256',
        _machine_material() + _TRUST_CONTEXT,
        salt,
        _TRUST_ITERATIONS,
        dklen=32,
    )


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    chunks: list[bytes] = []
    counter_index = 0
    while sum(len(chunk) for chunk in chunks) < length:
        counter = counter_index.to_bytes(8, 'big')
        chunks.append(hmac.new(key, nonce + counter, hashlib.sha256).digest())
        counter_index += 1
    return b''.join(chunks)[:length]


def _xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(left_byte ^ right_byte for left_byte, right_byte in zip(left, right))


def _encrypt_blob(plaintext: bytes, key: bytes, algorithm: str) -> dict[str, str | int]:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    ciphertext = _xor_bytes(plaintext, _keystream(key, nonce, len(plaintext)))
    signature = hmac.new(key, salt + nonce + ciphertext, hashlib.sha256).digest()
    return {
        'algorithm': algorithm,
        'salt': _base64_encode(salt),
        'nonce': _base64_encode(nonce),
        'ciphertext': _base64_encode(ciphertext),
        'mac': _base64_encode(signature),
    }


def _decrypt_blob(record: Any, key: bytes, algorithm: str) -> bytes:
    if not isinstance(record, dict):
        raise VaultPasswordError('Invalid encrypted record.')
    if record.get('algorithm') != algorithm:
        raise VaultPasswordError('Unsupported encrypted record.')

    try:
        salt = _base64_decode(str(record['salt']))
        nonce = _base64_decode(str(record['nonce']))
        ciphertext = _base64_decode(str(record['ciphertext']))
        expected_mac = _base64_decode(str(record['mac']))
        actual_mac = hmac.new(key, salt + nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_mac, actual_mac):
            raise VaultPasswordError('Wrong password or untrusted machine.')
        return _xor_bytes(ciphertext, _keystream(key, nonce, len(ciphertext)))
    except VaultPasswordError:
        raise
    except Exception as exc:
        raise VaultPasswordError('Could not decrypt vault data.') from exc


def _new_payload() -> dict[str, Any]:
    return {
        'schema_version': 2,
        'vault_id': secrets.token_hex(16),
        'created_at': _now(),
        'updated_at': _now(),
        'api_keys': {},
        'native_auth': None,
    }


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized.setdefault('schema_version', 2)
    normalized.setdefault('vault_id', secrets.token_hex(16))
    normalized.setdefault('created_at', _now())
    normalized.setdefault('api_keys', {})
    normalized.setdefault('native_auth', None)
    if not isinstance(normalized.get('api_keys'), dict):
        normalized['api_keys'] = {}
    normalized['updated_at'] = _now()
    return normalized


def _vault_record_for_payload(payload: dict[str, Any], key: bytes, salt: bytes, iterations: int) -> dict[str, Any]:
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    record = _encrypt_blob(plaintext, key, _VAULT_ALGORITHM)
    record['kdf'] = 'pbkdf2-hmac-sha256'
    record['iterations'] = iterations
    record['password_salt'] = _base64_encode(salt)
    return {
        'schema_version': 2,
        'vault': record,
    }


def _write_vault_payload(payload: dict[str, Any], key: bytes, salt: bytes | None = None, iterations: int | None = None) -> None:
    payload = _normalize_payload(payload)
    if salt is None or iterations is None:
        current = _read_secrets_file().get('vault', {})
        if isinstance(current, dict):
            salt = _base64_decode(str(current.get('password_salt', ''))) if current.get('password_salt') else secrets.token_bytes(16)
            iterations = int(current.get('iterations') or _VAULT_ITERATIONS)
        else:
            salt = secrets.token_bytes(16)
            iterations = _VAULT_ITERATIONS
    _write_json(secrets_path(), _vault_record_for_payload(payload, key, salt, iterations))


def _read_vault_record() -> dict[str, Any]:
    data = _read_secrets_file()
    vault = data.get('vault') if isinstance(data, dict) else None
    return vault if isinstance(vault, dict) else {}


def _decrypt_vault_with_key(key: bytes) -> dict[str, Any]:
    record = _read_vault_record()
    if not record:
        raise VaultLockedError('Secrets vault has not been created yet.')
    plaintext = _decrypt_blob(record, key, _VAULT_ALGORITHM)
    try:
        payload = json.loads(plaintext.decode('utf-8'))
    except Exception as exc:
        raise VaultPasswordError('Vault decrypted but payload is invalid.') from exc
    if not isinstance(payload, dict):
        raise VaultPasswordError('Vault payload is invalid.')
    return _normalize_payload(payload)


def _derive_key_for_vault(password: str) -> bytes:
    record = _read_vault_record()
    if not record:
        raise VaultLockedError('Secrets vault has not been created yet.')
    try:
        salt = _base64_decode(str(record['password_salt']))
        iterations = int(record.get('iterations') or _VAULT_ITERATIONS)
    except Exception as exc:
        raise VaultPasswordError('Vault metadata is invalid.') from exc
    return _derive_password_key(password, salt, iterations)


def _load_legacy_api_keys() -> dict[str, str]:
    data = _read_secrets_file()
    api_keys = data.get('api_keys') if isinstance(data, dict) else None
    if not isinstance(api_keys, dict):
        return {}

    migrated: dict[str, str] = {}
    for provider_key, record in api_keys.items():
        value = decrypt_secret(record)
        if value:
            migrated[str(provider_key)] = value
    return migrated


def _set_unlocked(payload: dict[str, Any], key: bytes) -> None:
    global _unlocked_payload, _unlocked_key, _vault_error
    _unlocked_payload = _normalize_payload(payload)
    _unlocked_key = key
    _vault_error = ''


def vault_exists() -> bool:
    return bool(_read_vault_record())


def is_vault_unlocked() -> bool:
    return _unlocked_payload is not None and _unlocked_key is not None


def lock_vault() -> None:
    global _unlocked_payload, _unlocked_key
    _unlocked_payload = None
    _unlocked_key = None


def vault_status() -> dict[str, Any]:
    if is_vault_unlocked():
        return {
            'state': 'unlocked',
            'message': 'Secrets vault unlocked.',
            'trusted': trust_path().exists(),
            'path': str(secrets_path()),
        }
    if _vault_error:
        return {
            'state': 'error',
            'message': _vault_error,
            'trusted': False,
            'path': str(secrets_path()),
        }
    if not vault_exists():
        return {
            'state': 'missing',
            'message': 'Secrets vault has not been created.',
            'trusted': False,
            'path': str(secrets_path()),
        }
    return {
        'state': 'locked',
        'message': 'Secrets vault locked.',
        'trusted': trust_path().exists(),
        'path': str(secrets_path()),
    }


def create_vault(password: str, trust_machine: bool = True, migrate_legacy: bool = True) -> None:
    if not password:
        raise VaultPasswordError('Vault password is required.')

    salt = secrets.token_bytes(16)
    key = _derive_password_key(password, salt, _VAULT_ITERATIONS)
    payload = _new_payload()
    if migrate_legacy:
        payload['api_keys'] = _load_legacy_api_keys()
    _write_json(secrets_path(), _vault_record_for_payload(payload, key, salt, _VAULT_ITERATIONS))
    _set_unlocked(payload, key)
    if trust_machine:
        trust_this_machine()


def unlock_vault(password: str, trust_machine: bool = False) -> None:
    global _vault_error
    if not password:
        _vault_error = 'Vault password is required.'
        raise VaultPasswordError(_vault_error)
    try:
        key = _derive_key_for_vault(password)
        payload = _decrypt_vault_with_key(key)
        _set_unlocked(payload, key)
        if trust_machine:
            trust_this_machine()
    except VaultError as exc:
        _vault_error = str(exc)
        lock_vault()
        raise


def _write_trust_file() -> None:
    if not is_vault_unlocked():
        raise VaultLockedError('Unlock the secrets vault before trusting this machine.')

    payload = _unlocked_payload or {}
    key = _unlocked_key or b''
    salt = secrets.token_bytes(16)
    trust_key = _derive_trust_key(salt)
    trust_payload = json.dumps({
        'vault_id': payload.get('vault_id'),
        'password_key': _base64_encode(key),
        'created_at': _now(),
    }, separators=(',', ':')).encode('utf-8')
    record = _encrypt_blob(trust_payload, trust_key, _TRUST_ALGORITHM)
    record.update({
        'schema_version': 1,
        'machine_hash': _machine_hash(),
        'iterations': _TRUST_ITERATIONS,
        'trust_salt': _base64_encode(salt),
    })
    _write_json(trust_path(), record)


def trust_this_machine() -> None:
    _write_trust_file()


def forget_trusted_machine() -> None:
    try:
        trust_path().unlink()
    except FileNotFoundError:
        pass


def try_unlock_with_trusted_machine() -> bool:
    global _vault_error
    if is_vault_unlocked():
        return True
    if not vault_exists() or not trust_path().exists():
        return False

    record = _read_json(trust_path(), {})
    try:
        if record.get('algorithm') != _TRUST_ALGORITHM:
            return False
        if record.get('machine_hash') != _machine_hash():
            return False
        salt = _base64_decode(str(record['trust_salt']))
        trust_key = _derive_trust_key(salt)
        plaintext = _decrypt_blob(record, trust_key, _TRUST_ALGORITHM)
        data = json.loads(plaintext.decode('utf-8'))
        key = _base64_decode(str(data['password_key']))
        payload = _decrypt_vault_with_key(key)
        if str(data.get('vault_id')) != str(payload.get('vault_id')):
            return False
        _set_unlocked(payload, key)
        return True
    except Exception:
        _vault_error = 'Trusted-machine unlock failed. Re-enter the vault password.'
        lock_vault()
        return False


def change_vault_password(current_password: str, new_password: str, trust_machine: bool = True) -> None:
    if not new_password:
        raise VaultPasswordError('New vault password is required.')
    unlock_vault(current_password, trust_machine=False)
    payload = _unlocked_payload or _new_payload()
    salt = secrets.token_bytes(16)
    key = _derive_password_key(new_password, salt, _VAULT_ITERATIONS)
    _write_json(secrets_path(), _vault_record_for_payload(payload, key, salt, _VAULT_ITERATIONS))
    _set_unlocked(payload, key)
    if trust_machine:
        trust_this_machine()
    else:
        forget_trusted_machine()


def _require_payload() -> dict[str, Any]:
    if not is_vault_unlocked():
        raise VaultLockedError('Secrets vault is locked. Unlock it from the tray menu.')
    return _unlocked_payload or {}


def _save_unlocked_payload() -> None:
    if not is_vault_unlocked() or _unlocked_payload is None or _unlocked_key is None:
        raise VaultLockedError('Secrets vault is locked. Unlock it from the tray menu.')
    _write_vault_payload(_unlocked_payload, _unlocked_key)


def encrypt_secret(value: str) -> dict[str, str]:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    key = _derive_master_key(salt)
    plaintext = value.encode('utf-8')
    ciphertext = _xor_bytes(plaintext, _keystream(key, nonce, len(plaintext)))
    signature = hmac.new(key, salt + nonce + ciphertext, hashlib.sha256).digest()
    return {
        'algorithm': _LEGACY_SECRET_ALGORITHM,
        'salt': _base64_encode(salt),
        'nonce': _base64_encode(nonce),
        'ciphertext': _base64_encode(ciphertext),
        'mac': _base64_encode(signature),
    }


def decrypt_secret(record: Any) -> str:
    if isinstance(record, str):
        return record
    if not isinstance(record, dict):
        return ''
    if record.get('algorithm') != _LEGACY_SECRET_ALGORITHM:
        return ''

    try:
        salt = _base64_decode(str(record['salt']))
        key = _derive_master_key(salt)
        plaintext = _decrypt_blob(record, key, _LEGACY_SECRET_ALGORITHM)
        return plaintext.decode('utf-8')
    except Exception:
        return ''


def get_api_key(provider_key: str) -> str:
    if not is_vault_unlocked():
        try_unlock_with_trusted_machine()
    if not is_vault_unlocked():
        return ''

    payload = _require_payload()
    api_keys = payload.get('api_keys', {})
    if not isinstance(api_keys, dict):
        return ''
    value = api_keys.get(provider_key)
    return str(value) if isinstance(value, str) else ''


def set_api_key(provider_key: str, api_key: str) -> None:
    payload = _require_payload()
    api_keys = payload.setdefault('api_keys', {})
    if not isinstance(api_keys, dict):
        api_keys = {}
        payload['api_keys'] = api_keys

    if api_key:
        api_keys[provider_key] = api_key
    else:
        api_keys.pop(provider_key, None)
    _save_unlocked_payload()


def remove_api_key(provider_key: str) -> None:
    set_api_key(provider_key, '')


def get_native_auth() -> dict[str, Any]:
    if not is_vault_unlocked():
        try_unlock_with_trusted_machine()
    if not is_vault_unlocked():
        return {}
    auth = _require_payload().get('native_auth')
    return dict(auth) if isinstance(auth, dict) else {}


def set_native_auth(auth_data: dict[str, Any] | None) -> None:
    payload = _require_payload()
    payload['native_auth'] = dict(auth_data) if isinstance(auth_data, dict) and auth_data else None
    _save_unlocked_payload()


def has_native_auth() -> bool:
    return bool(get_native_auth())