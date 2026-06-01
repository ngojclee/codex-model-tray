"""
Provider and model catalog for Codex Model Tray.

Provider/model metadata is loaded from codex_model_tray.catalog.json next to the
app executable. API keys are intentionally not stored here; they live in the
portable secrets file managed by portable_settings.py.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import portable_settings as settings


VALID_AUTH_MODES = {'chatgpt', 'apikey', 'none'}
DEFAULT_PROVIDER_KEY = 'cliproxy'

_EMPTY_CATALOG: dict[str, Any] = {
    'schema_version': 1,
    'default_provider_key': 'cliproxy',
    'provider_aliases': {},
    'providers': [],
    'models': [],
    'pinned_models': [],
}


def ensure_catalog_file() -> None:
    target = settings.catalog_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return

    candidates = [
        Path(__file__).resolve().parent / settings.CATALOG_FILENAME,
        Path(getattr(sys, '_MEIPASS', '')) / settings.CATALOG_FILENAME,
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.resolve() != target.resolve():
            try:
                shutil.copyfile(candidate, target)
                return
            except Exception:
                pass


def catalog_file_path() -> str:
    return str(settings.catalog_path())


def secrets_file_path() -> str:
    return str(settings.secrets_path())


def settings_folder_path() -> str:
    return str(settings.settings_dir())


def _read_catalog() -> dict[str, Any]:
    ensure_catalog_file()
    data = settings.read_catalog()
    if not data.get('default_provider_key'):
        data['default_provider_key'] = _EMPTY_CATALOG['default_provider_key']
    if not isinstance(data.get('provider_aliases'), dict):
        data['provider_aliases'] = dict(_EMPTY_CATALOG['provider_aliases'])
    if not isinstance(data.get('pinned_models'), list):
        data['pinned_models'] = list(_EMPTY_CATALOG['pinned_models'])
    return data


def _write_catalog(data: dict[str, Any]) -> None:
    settings.write_catalog(data)


def _provider_aliases(data: dict[str, Any] | None = None) -> dict[str, str]:
    source = data or _read_catalog()
    raw = source.get('provider_aliases', {})
    if not isinstance(raw, dict):
        return {}
    aliases: dict[str, str] = {}
    for alias, target in raw.items():
        if isinstance(alias, str) and isinstance(target, str) and alias and target:
            aliases[alias] = target
    return aliases


def normalize_provider_key(provider_key: str | None) -> str:
    data = _read_catalog()
    key = (provider_key or data.get('default_provider_key') or DEFAULT_PROVIDER_KEY).strip()
    aliases = _provider_aliases(data)
    return aliases.get(key, key)


def _normalize_auth_mode(auth_mode: str | None) -> str:
    normalized = (auth_mode or 'apikey').strip().lower().replace('-', '_')
    if normalized == 'api_key':
        normalized = 'apikey'
    return normalized if normalized in VALID_AUTH_MODES else 'apikey'


def _normalize_provider(provider: dict[str, Any]) -> dict[str, Any] | None:
    provider_id = str(provider.get('id', '')).strip()
    if not provider_id:
        return None
    auth_mode = _normalize_auth_mode(str(provider.get('auth_mode', 'apikey')))
    label = str(provider.get('label') or provider.get('name') or provider_id)
    normalized: dict[str, Any] = {
        'id': provider_id,
        'label': label,
        'name': label,
        'icon': str(provider.get('icon') or '•'),
        'auth_mode': auth_mode,
        'wire_api': str(provider.get('wire_api') or 'responses'),
        'db_provider': str(provider.get('db_provider') or provider_id),
        'config_provider': str(provider.get('config_provider') or provider.get('db_provider') or provider_id),
        'builtin': bool(provider.get('builtin', False)),
        'custom': bool(provider.get('custom', False)),
    }
    base_url = str(provider.get('base_url') or '').strip()
    if base_url:
        normalized['base_url'] = base_url
    env_key = str(provider.get('env_key') or '').strip()
    if env_key:
        normalized['env_key'] = env_key
    if 'models' in provider and isinstance(provider['models'], list):
        normalized['models'] = [str(model) for model in provider['models'] if str(model).strip()]
    return normalized


def _catalog_providers(data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    source = data or _read_catalog()
    raw = source.get('providers', [])
    if not isinstance(raw, list):
        return []
    providers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        provider = _normalize_provider(item)
        if not provider or provider['id'] in seen:
            continue
        seen.add(provider['id'])
        providers.append(provider)
    return providers


def get_provider_catalog() -> dict[str, dict[str, Any]]:
    return {provider['id']: dict(provider) for provider in _catalog_providers()}


def list_providers() -> list[tuple[str, str]]:
    return [(provider['id'], dict(provider)) for provider in _catalog_providers()]


def get_provider(provider_key: str | None) -> dict[str, Any] | None:
    key = normalize_provider_key(provider_key)
    for provider in _catalog_providers():
        if provider['id'] == key:
            return dict(provider)
    return None


def get_db_provider_key(provider_key: str | None) -> str:
    provider = get_provider(provider_key)
    if provider:
        return str(provider.get('db_provider') or provider['id'])
    return normalize_provider_key(provider_key)


def get_last_provider() -> str:
    data = _read_catalog()
    key = data.get('last_provider_key') or data.get('default_provider_key') or DEFAULT_PROVIDER_KEY
    normalized = normalize_provider_key(str(key))
    return normalized if get_provider(normalized) else DEFAULT_PROVIDER_KEY


def set_last_provider(provider_key: str) -> None:
    normalized = normalize_provider_key(provider_key)
    data = _read_catalog()
    data['last_provider_key'] = normalized
    _write_catalog(data)


def get_custom_providers() -> list[dict[str, Any]]:
    custom_providers: list[dict[str, Any]] = []
    secrets_unlocked = settings.is_vault_unlocked() or settings.try_unlock_with_trusted_machine()
    for provider in _catalog_providers():
        if provider.get('builtin', False):
            continue
        item = dict(provider)
        item['api_key'] = settings.get_api_key(item['id']) if secrets_unlocked else ''
        item['secrets_unlocked'] = secrets_unlocked
        custom_providers.append(item)
    return custom_providers


def upsert_custom_provider(
    provider_id: str | dict[str, Any],
    name: str = '',
    base_url: str = '',
    api_key: str = '',
    auth_mode: str = 'apikey',
    env_key: str = 'OPENAI_API_KEY',
) -> tuple[bool, str, str]:
    if isinstance(provider_id, dict):
        data = provider_id
        provider_id = str(data.get('id', ''))
        name = str(data.get('label') or data.get('name') or name)
        base_url = str(data.get('base_url') or base_url)
        api_key = str(data.get('api_key') or api_key)
        auth_mode = str(data.get('auth_mode') or auth_mode)
        env_key = str(data.get('env_key') or env_key)

    provider_id = provider_id.strip()
    if not provider_id:
        return False, 'Provider ID is required.', ''

    normalized = _normalize_provider({
        'id': provider_id,
        'name': name.strip() or provider_id,
        'base_url': base_url.strip(),
        'wire_api': 'responses',
        'auth_mode': auth_mode,
        'env_key': env_key.strip() or 'OPENAI_API_KEY',
        'db_provider': provider_id,
        'config_provider': provider_id,
        'custom': True,
    })
    if not normalized:
        return False, 'Provider ID is required.', ''

    if api_key and not (settings.is_vault_unlocked() or settings.try_unlock_with_trusted_machine()):
        return False, 'Unlock the secrets vault before saving an API key.', ''

    data = _read_catalog()
    providers = _catalog_providers(data)
    updated = False
    new_providers: list[dict[str, Any]] = []
    for existing in providers:
        if existing['id'] == provider_id:
            new_providers.append(normalized)
            updated = True
        else:
            new_providers.append(existing)
    if not updated:
        new_providers.append(normalized)

    data['providers'] = new_providers
    _write_catalog(data)
    if api_key:
        settings.set_api_key(provider_id, api_key)
    return True, f'Saved provider "{provider_id}".', provider_id


def remove_custom_provider(provider_id: str) -> bool:
    provider_id = normalize_provider_key(provider_id)
    existing_provider = get_provider(provider_id)
    if existing_provider and existing_provider.get('builtin'):
        return False

    data = _read_catalog()
    providers = _catalog_providers(data)
    new_providers = [provider for provider in providers if provider['id'] != provider_id]
    removed = len(new_providers) != len(providers)
    if removed:
        data['providers'] = new_providers
        if data.get('last_provider_key') == provider_id:
            data['last_provider_key'] = data.get('default_provider_key') or DEFAULT_PROVIDER_KEY
        _write_catalog(data)
        if settings.is_vault_unlocked() or settings.try_unlock_with_trusted_machine():
            settings.remove_api_key(provider_id)
    return removed


def get_model_catalog() -> list[tuple[str, str, str]]:
    data = _read_catalog()
    raw = data.get('models', [])
    if not isinstance(raw, list):
        raw = []

    models: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            model_id = str(item.get('id', '')).strip()
            description = str(item.get('description') or item.get('desc') or '')
            icon = str(item.get('icon') or '•')
        elif isinstance(item, (list, tuple)) and item:
            model_id = str(item[0]).strip()
            description = str(item[1]) if len(item) > 1 else ''
            icon = str(item[2]) if len(item) > 2 else '•'
        else:
            continue

        if model_id and model_id not in seen:
            seen.add(model_id)
            models.append((model_id, description, icon))
    return models


def add_model(model_id: str, description: str = 'Custom model', icon: str = '•') -> bool:
    model_id = model_id.strip()
    if not model_id:
        return False

    data = _read_catalog()
    catalog = get_model_catalog()
    if any(existing_id == model_id for existing_id, _, _ in catalog):
        return False

    models = data.get('models') if isinstance(data.get('models'), list) else []
    models.append({'id': model_id, 'description': description, 'icon': icon})
    data['models'] = models
    _write_catalog(data)
    return True


def remove_model(model_id: str) -> bool:
    data = _read_catalog()
    raw = data.get('models') if isinstance(data.get('models'), list) else []
    new_raw: list[Any] = []
    removed = False
    for item in raw:
        item_id = ''
        if isinstance(item, dict):
            item_id = str(item.get('id', '')).strip()
        elif isinstance(item, (list, tuple)) and item:
            item_id = str(item[0]).strip()
        if item_id == model_id:
            removed = True
            continue
        new_raw.append(item)

    if removed:
        data['models'] = new_raw
        pins = data.get('pinned_models', [])
        if isinstance(pins, list):
            data['pinned_models'] = [pin for pin in pins if pin != model_id]
        _write_catalog(data)
    return removed


def get_models_for_provider(provider_key: str | None = None) -> list[tuple[str, str, str]]:
    provider = get_provider(provider_key) if provider_key else None
    provider_models = provider.get('models') if provider else None
    if not provider_models:
        return get_model_catalog()

    allowed = {str(model_id) for model_id in provider_models}
    return [model for model in get_model_catalog() if model[0] in allowed]


def get_pinned_models() -> list[str]:
    data = _read_catalog()
    pins = data.get('pinned_models')
    if isinstance(pins, list):
        return [str(model_id) for model_id in pins if str(model_id).strip()]
    return []


def set_pinned_models(pins: list[str]) -> None:
    data = _read_catalog()
    known_models = {model_id for model_id, _, _ in get_model_catalog()}
    cleaned: list[str] = []
    for model_id in pins:
        if model_id in known_models and model_id not in cleaned:
            cleaned.append(model_id)
    data['pinned_models'] = cleaned
    _write_catalog(data)


def toggle_pinned_model(model_id: str) -> list[str]:
    pins = get_pinned_models()
    if model_id in pins:
        pins.remove(model_id)
    else:
        pins.append(model_id)
    set_pinned_models(pins)
    return get_pinned_models()


def get_secret_api_key(provider_key: str) -> str:
    return settings.get_api_key(normalize_provider_key(provider_key))


def set_secret_api_key(provider_key: str, api_key: str) -> None:
    settings.set_api_key(normalize_provider_key(provider_key), api_key)