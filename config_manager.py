"""
Config manager for Codex config.toml and auth.json.

Strategy: keep edits surgical and text-based so existing config structure is
preserved as much as possible, while still being resilient on first run when
files may be missing or incomplete.
"""

from __future__ import annotations

import json
import os
import re
import traceback
from datetime import datetime, timezone

import portable_settings as secret_store
from providers import (
    DEFAULT_PROVIDER_KEY,
    get_pinned_models as provider_get_pinned_models,
    get_last_provider,
    get_provider,
    get_secret_api_key,
    normalize_provider_key,
    set_secret_api_key,
    set_last_provider,
    toggle_pinned_model as provider_toggle_pinned_model,
    upsert_custom_provider,
)


def _codex_home() -> str:
    if e := os.environ.get('CODEX_HOME'):
        return e
    return os.path.join(os.path.expanduser('~'), '.codex')


def _ensure_codex_home() -> None:
    os.makedirs(_codex_home(), exist_ok=True)


def config_path() -> str:
    return os.path.join(_codex_home(), 'config.toml')


def auth_path() -> str:
    return os.path.join(_codex_home(), 'auth.json')


def _default_config_content(model: str = 'gpt-5.4') -> str:
    return (
        f'model = "{model}"\n'
        'sandbox_mode = "danger-full-access"\n'
        'approval_policy = "never"\n'
    )


def _load_config_content(default_model: str = 'gpt-5.4') -> str:
    content = read_config()
    content = content.lstrip('\ufeff')
    if _extract_model(content):
        return content
    return _default_config_content(default_model) + content


def _normalize_newlines(content: str) -> str:
    content = content.replace('\r\n', '\n')
    content = re.sub(r'\n{3,}', '\n\n', content)
    return content.rstrip() + '\n'


def _split_top_level(content: str) -> tuple[str, str]:
    match = re.search(r'^\[', content, re.MULTILINE)
    if not match:
        return content, ''
    return content[:match.start()], content[match.start():]


def _top_level_string_line_re(field: str) -> re.Pattern[str]:
    return re.compile(rf'^\s*{re.escape(field)}\s*=\s*"[^"]*"\s*(?:#.*)?$')


def _extract_top_level_string(content: str, field: str) -> str | None:
    top_level, _rest = _split_top_level(content)
    match = re.search(
        rf'^\s*{re.escape(field)}\s*=\s*"([^"]*)"\s*(?:#.*)?$',
        top_level,
        re.MULTILINE,
    )
    return match.group(1) if match else None


def _rewrite_top_level_string_field(
    content: str,
    field: str,
    value: str | None = None,
) -> tuple[str, bool]:
    top_level, rest = _split_top_level(content)
    pattern = _top_level_string_line_re(field)
    found = False
    lines: list[str] = []

    for line in top_level.splitlines(keepends=True):
        body = line.rstrip('\r\n')
        newline = line[len(body):]
        if pattern.match(body):
            if found:
                continue
            found = True
            lines.append(line if value is None else f'{field} = "{value}"{newline}')
            continue
        lines.append(line)

    return ''.join(lines) + rest, found


def _remove_top_level_string_field(content: str, field: str) -> str:
    top_level, rest = _split_top_level(content)
    pattern = _top_level_string_line_re(field)
    lines = [
        line for line in top_level.splitlines(keepends=True)
        if not pattern.match(line.rstrip('\r\n'))
    ]
    return ''.join(lines) + rest


def _insert_top_level_string_lines(content: str, lines: list[str]) -> str:
    if not lines:
        return content

    insertion = '\n'.join(lines)
    top_level, rest = _split_top_level(content)
    updated, count = re.subn(
        r'(^\s*model\s*=\s*"[^"]*"\s*(?:#.*)?$)',
        lambda match: match.group(1) + '\n' + insertion,
        top_level,
        count=1,
        flags=re.MULTILINE,
    )
    if count:
        return updated + rest

    return insertion + '\n' + content.lstrip()


def _extract_model(content: str) -> str | None:
    return _extract_top_level_string(content, 'model')


# ── Pinned Models ───────────────────────────────────────────

def get_pinned_models() -> list[str]:
    return provider_get_pinned_models()


def toggle_pinned_model(model_id: str) -> list[str]:
    return provider_toggle_pinned_model(model_id)


# ── API Key Management ──────────────────────────────────────

def get_custom_api_key(provider_key: str) -> str | None:
    key = get_secret_api_key(provider_key)
    return key or None


def set_custom_api_key(provider_key: str, api_key: str) -> None:
    set_secret_api_key(provider_key, api_key)


def get_effective_api_key(provider_key: str) -> str:
    custom = get_custom_api_key(provider_key)
    if custom:
        return custom
    provider = get_provider(provider_key) or {}
    return provider.get('api_key', '')


# ── Workspace Roots (Codex Desktop Projects) ───────────────

def _global_state_path() -> str:
    return os.path.join(_codex_home(), '.codex-global-state.json')


def _read_global_state() -> dict:
    try:
        with open(_global_state_path(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _write_global_state(data: dict) -> None:
    _ensure_codex_home()
    with open(_global_state_path(), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def get_workspace_roots() -> list[str]:
    gs = _read_global_state()
    roots = gs.get('electron-saved-workspace-roots', [])
    return roots if isinstance(roots, list) else []


def set_workspace_roots(roots: list[str]) -> bool:
    try:
        gs = _read_global_state()
        gs['electron-saved-workspace-roots'] = roots
        gs['project-order'] = roots
        _write_global_state(gs)
        return True
    except Exception as e:
        print(f'[ConfigManager] set_workspace_roots failed: {e}')
        return False


def fix_missing_projects() -> tuple[bool, int, int]:
    try:
        current = get_workspace_roots()
        db_projects = _extract_projects_from_db()

        merged = list(current)
        added_count = 0
        for project in db_projects:
            if project not in merged:
                merged.append(project)
                added_count += 1

        if added_count > 0:
            success = set_workspace_roots(merged)
            return success, len(merged), added_count
        return True, len(merged), 0
    except Exception as e:
        print(f'[ConfigManager] fix_missing_projects failed: {e}')
        return False, 0, 0


def _extract_projects_from_db() -> list[str]:
    import sqlite3

    db_path = os.path.join(_codex_home(), 'state_5.sqlite')
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT cwd, COUNT(*) as cnt
            FROM threads WHERE archived = 0
            GROUP BY cwd ORDER BY cnt DESC
            """
        )
        seen = {}
        for cwd, _cnt in cur.fetchall():
            if not cwd:
                continue
            clean = cwd.replace('\\\\?\\', '').replace('/', '\\')
            key = clean.lower()
            if key not in seen:
                seen[key] = clean
        conn.close()
        return [value for value in seen.values() if os.path.exists(value)]
    except Exception:
        return []


# ── Read helpers ────────────────────────────────────────────

def read_config() -> str:
    try:
        with open(config_path(), 'r', encoding='utf-8') as f:
            return f.read()
    except Exception:
        return _default_config_content()


def get_current_model() -> str:
    return _extract_model(read_config()) or 'unknown'


def get_current_provider() -> str:
    content = read_config()
    provider_blocks = re.findall(r'^\[model_providers\.([^\]]+)\]', content, re.MULTILINE)
    normalized_block_ids = {
        normalize_provider_key(provider_key)
        for provider_key in provider_blocks
    }

    top_level_provider = _extract_top_level_string(content, 'model_provider')
    if top_level_provider:
        provider = normalize_provider_key(top_level_provider)
        if get_provider(provider) or provider in normalized_block_ids:
            return provider

    last_provider = get_last_provider()
    if last_provider == 'native' and get_provider(last_provider):
        return last_provider

    normalized_blocks = [
        provider_key
        for provider_key in normalized_block_ids
        if get_provider(provider_key) or provider_key
    ]

    auth = read_auth()
    if auth.get('auth_mode') == 'chatgpt' and not normalized_blocks:
        return 'native'

    if last_provider and get_provider(last_provider):
        return last_provider

    if len(normalized_blocks) == 1:
        return normalized_blocks[0]

    return 'native' if auth.get('auth_mode') == 'chatgpt' else DEFAULT_PROVIDER_KEY


def read_auth() -> dict:
    try:
        with open(auth_path(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _vault_ready() -> bool:
    return secret_store.is_vault_unlocked() or secret_store.try_unlock_with_trusted_machine()


def _vault_required_message() -> str:
    status = secret_store.vault_status()
    if status.get('state') == 'missing':
        return 'Create the secrets vault from the tray menu before switching providers.'
    return 'Unlock the secrets vault from the tray menu before switching providers.'


def _auth_is_native(auth_data: dict) -> bool:
    return isinstance(auth_data, dict) and auth_data.get('auth_mode') == 'chatgpt'


def backup_native_auth() -> tuple[bool, str]:
    auth_data = read_auth()
    if not _auth_is_native(auth_data):
        return True, 'No native auth to back up.'
    if not _vault_ready():
        return False, 'Unlock the secrets vault before switching away from native auth.'
    try:
        secret_store.set_native_auth(auth_data)
        return True, 'Native auth backed up to the secrets vault.'
    except Exception as exc:
        return False, f'Could not back up native auth: {exc}'


def restore_native_auth() -> tuple[dict, bool]:
    if secret_store.vault_exists() and not _vault_ready():
        raise secret_store.VaultLockedError(_vault_required_message())
    auth_data = secret_store.get_native_auth()
    if auth_data:
        return auth_data, True
    current = read_auth()
    if _auth_is_native(current):
        return current, False
    return {'auth_mode': 'chatgpt'}, False


# ── Write helpers ───────────────────────────────────────────

def _write_config(content: str) -> None:
    _ensure_codex_home()
    _audit_config_write(content)
    with open(config_path(), 'w', encoding='utf-8') as f:
        f.write(_normalize_newlines(content))


def _auth_audit_path() -> str:
    log_dir = os.path.join(str(secret_store.settings_dir()), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, 'auth-writes.log')


def _config_audit_path() -> str:
    log_dir = os.path.join(str(secret_store.settings_dir()), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, 'config-writes.log')


def _observed_change_audit_path() -> str:
    log_dir = os.path.join(str(secret_store.settings_dir()), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, 'observed-file-changes.log')


def _audit_auth_write(data: dict) -> None:
    try:
        caller_lines = traceback.format_stack(limit=5)[:-1]
        mode = data.get('auth_mode') if isinstance(data, dict) else '<invalid>'
        provider = get_current_provider()
        event = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'pid': os.getpid(),
            'auth_mode': mode,
            'provider': provider,
            'keys': sorted(data.keys()) if isinstance(data, dict) else [],
            'caller': ''.join(caller_lines).strip(),
        }
        with open(_auth_audit_path(), 'a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
    except Exception:
        pass


def _audit_config_write(content: str) -> None:
    try:
        caller_lines = traceback.format_stack(limit=5)[:-1]
        event = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'pid': os.getpid(),
            'model': _extract_model(content),
            'model_provider': _extract_model_provider(content),
            'bytes': len(content.encode('utf-8')),
            'caller': ''.join(caller_lines).strip(),
        }
        with open(_config_audit_path(), 'a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
    except Exception:
        pass


def audit_observed_file_change(name: str, path: str) -> None:
    try:
        stat = os.stat(path)
        event = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'pid': os.getpid(),
            'name': name,
            'path': path,
            'mtime': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            'bytes': stat.st_size,
        }
    except OSError:
        event = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'pid': os.getpid(),
            'name': name,
            'path': path,
            'missing': True,
        }

    try:
        with open(_observed_change_audit_path(), 'a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
    except Exception:
        pass


def _write_auth(data: dict) -> None:
    _ensure_codex_home()
    _audit_auth_write(data)
    target = auth_path()
    temp_path = f'{target}.tmp.{os.getpid()}'
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    os.replace(temp_path, target)


def ensure_config_file() -> None:
    if not os.path.exists(config_path()):
        _write_config(_default_config_content())


def ensure_auth_file() -> None:
    if not os.path.exists(auth_path()):
        _write_auth({'auth_mode': 'chatgpt'})


def _remove_toml_section(content: str, section_name: str) -> str:
    escaped = re.escape(section_name)
    pattern = rf'^\[{escaped}\]\n(?:^(?!\[).*\n?)*'
    return re.sub(pattern, '', content, flags=re.MULTILINE)


def _remove_provider_sections(content: str) -> str:
    return re.sub(
        r'^\[model_providers\.[^\]]+\]\n(?:^(?!\[).*\n?)*',
        '',
        content,
        flags=re.MULTILINE,
    )


def _remove_provider_secret_fields(content: str) -> str:
    lines = []
    in_provider_block = False

    for line in content.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith('['):
            in_provider_block = stripped.startswith('[model_providers.')
        if in_provider_block and re.match(r'\s*api_key\s*=', line):
            continue
        lines.append(line)

    return ''.join(lines)


def _ensure_model_line(content: str, model: str) -> str:
    if _extract_model(content):
        content, _found = _rewrite_top_level_string_field(content, 'model')
        return content
    return f'model = "{model}"\n' + content.lstrip()


def _set_top_level_model(content: str, model: str) -> str:
    content = _ensure_model_line(content, model)
    content, _found = _rewrite_top_level_string_field(content, 'model', model)
    return content


def _upsert_top_level_string_fields(content: str, fields: list[tuple[str, str]]) -> str:
    missing_lines: list[str] = []
    for field, value in fields:
        content, found = _rewrite_top_level_string_field(content, field, value)
        if not found:
            missing_lines.append(f'{field} = "{value}"')

    if missing_lines:
        content = _insert_top_level_string_lines(content, missing_lines)
    return content


def _ensure_top_level_string_fields(content: str, fields: list[tuple[str, str]]) -> str:
    missing_lines: list[str] = []
    for field, value in fields:
        content, found = _rewrite_top_level_string_field(content, field)
        if not found:
            missing_lines.append(f'{field} = "{value}"')

    if missing_lines:
        content = _insert_top_level_string_lines(content, missing_lines)
    return content


def _insert_before_sections(content: str, block: str, section_prefixes: tuple[str, ...]) -> str:
    positions = []
    for prefix in section_prefixes:
        match = re.search(rf'^\[{re.escape(prefix)}', content, re.MULTILINE)
        if match:
            positions.append(match.start())
    if positions:
        insert_at = min(positions)
        return content[:insert_at] + block + content[insert_at:]
    if not content.endswith('\n'):
        content += '\n'
    return content + block


def _section_block(lines: list[str]) -> str:
    return '\n' + '\n'.join(lines) + '\n\n'


def _upsert_subagent_block(content: str, provider_key: str, model: str | None) -> str:
    content = _remove_toml_section(content, 'agents.subagent')
    if not model:
        return content
    provider = get_provider(provider_key)
    lines = [
        '[agents.subagent]',
        f'model = "{model}"',
    ]
    if provider and provider.get('auth_mode') != 'chatgpt':
        lines.append(f'model_provider = "{provider_key}"')
    block = _section_block(lines)
    return _insert_before_sections(content, block, ('mcp_servers', 'windows', 'projects', 'features'))


def _upsert_provider_block(content: str, provider_key: str, provider: dict) -> str:
    content = _remove_toml_section(content, f'model_providers.{provider_key}')
    lines = [
        f'[model_providers.{provider_key}]',
        f'name = "{provider["label"]}"',
    ]
    if provider.get('base_url'):
        lines.append(f'base_url = "{provider["base_url"]}"')
    if provider.get('wire_api'):
        lines.append(f'wire_api = "{provider["wire_api"]}"')
    block = _section_block(lines)
    return _insert_before_sections(content, block, ('windows', 'projects', 'features'))


def _sync_subagent_provider(content: str, provider_key: str) -> str:
    match = re.search(
        r'^\[agents\.subagent\]\n(?:^(?!\[).*\n?)*',
        content,
        flags=re.MULTILINE,
    )
    if not match:
        return content

    block = match.group(0)
    provider = get_provider(provider_key)
    lines = [line for line in block.rstrip('\n').split('\n') if line]
    updated_lines: list[str] = []
    inserted = False

    for line in lines:
        if re.match(r'\s*model_provider\s*=', line):
            if (
                provider
                and provider.get('auth_mode') != 'chatgpt'
                and not inserted
            ):
                updated_lines.append(f'model_provider = "{provider_key}"')
                inserted = True
            # If already inserted (or switching to chatgpt) drop the line.
            continue
        updated_lines.append(line)
        if (
            re.match(r'\s*model\s*=', line)
            and provider
            and provider.get('auth_mode') != 'chatgpt'
            and not inserted
        ):
            updated_lines.append(f'model_provider = "{provider_key}"')
            inserted = True

    new_block = '\n'.join(updated_lines) + '\n\n'
    start, end = match.span()
    return content[:start] + new_block + content[end:]


def _remove_reasoning_config(content: str) -> str:
    content = _remove_top_level_string_field(content, 'model_reasoning_effort')
    content = _remove_top_level_string_field(content, 'model_reasoning_summary')
    return content


def _normalize_provider_config(
    content: str,
    provider_key: str,
) -> str:
    provider = get_provider(provider_key)
    if not provider:
        return content

    model = _extract_model(content) or 'gpt-5.4'
    content = _ensure_model_line(content, model)
    content = _remove_provider_secret_fields(content)

    if provider.get('auth_mode') == 'chatgpt':
        content = _remove_top_level_string_field(content, 'model_provider')
        content = _remove_provider_sections(content)
        content = _sync_subagent_provider(content, provider_key)
        return content

    content = _remove_reasoning_config(content)
    content = _upsert_top_level_string_fields(content, [
        ('model_provider', provider_key),
    ])
    content = _ensure_top_level_string_fields(content, [
        ('sandbox_mode', 'danger-full-access'),
        ('approval_policy', 'never'),
    ])
    content = _upsert_provider_block(content, provider_key, provider)
    content = _sync_subagent_provider(content, provider_key)
    return content


# ── Subagent management ──────────────────────────────────────

def get_subagent_model() -> str | None:
    content = read_config()
    match = re.search(r'\[agents\.subagent\](?:[^\[]*\n)*?model\s*=\s*"([^"]+)"', content)
    return match.group(1) if match else None


def remove_subagent() -> bool:
    try:
        content = _load_config_content()
        content = _remove_toml_section(content, 'agents.subagent')
        _write_config(content)
        return True
    except Exception as e:
        print(f'[ConfigManager] remove_subagent failed: {e}')
        return False


def add_or_update_subagent(new_model: str) -> bool:
    try:
        content = _load_config_content(new_model)
        provider_key = get_current_provider()
        content = _upsert_subagent_block(content, provider_key, new_model)
        _write_config(content)
        return True
    except Exception as e:
        print(f'[ConfigManager] update_subagent failed: {e}')
        return False


# ── Model switching ─────────────────────────────────────────

def set_model(new_model: str) -> bool:
    try:
        content = _load_config_content(new_model)
        content = _set_top_level_model(content, new_model)
        _write_config(content)
        return True
    except Exception as e:
        print(f'[ConfigManager] set_model failed: {e}')
        return False


# ── Provider switching ──────────────────────────────────────

def switch_to_custom_provider(provider_key: str) -> tuple[bool, str]:
    provider = get_provider(provider_key)
    if not provider or provider.get('auth_mode') == 'chatgpt':
        return False, 'Selected provider is not an API-key provider.'

    if not _vault_ready():
        return False, _vault_required_message()

    backup_ok, backup_message = backup_native_auth()
    if not backup_ok:
        return False, backup_message

    api_key = get_effective_api_key(provider_key)
    if not provider.get('base_url'):
        return False, f'Provider "{provider_key}" is missing a base URL.'
    if not api_key:
        return False, f'Provider "{provider_key}" is missing an API key.'

    try:
        _write_auth({
            'auth_mode': 'apikey',
            'OPENAI_API_KEY': api_key,
        })

        current_model = get_current_model()
        target_model = current_model if current_model != 'unknown' else 'gpt-5.4'
        content = _load_config_content(target_model)
        content = _normalize_provider_config(content, provider_key)
        _write_config(content)
        set_last_provider(provider_key)
        return True, f'Switched to {provider["label"]}.'
    except Exception as e:
        print(f'[ConfigManager] switch_to_custom_provider failed: {e}')
        return False, str(e)


def switch_to_native(provider_key: str = 'native') -> tuple[bool, str]:
    provider = get_provider(provider_key) or get_provider('native')
    if not provider:
        return False, 'Native provider is unavailable.'

    try:
        native_auth, restored = restore_native_auth()
        _write_auth(native_auth)

        current_model = get_current_model()
        target_model = current_model if current_model != 'unknown' else 'gpt-5.4'
        content = _load_config_content(target_model)
        content = _normalize_provider_config(content, 'native')
        _write_config(content)
        set_last_provider(provider_key)
        suffix = ' Restored native auth from vault.' if restored else ' Native login mode is active.'
        return True, f'Switched to {provider["label"]}.{suffix}'
    except Exception as e:
        print(f'[ConfigManager] switch_to_native failed: {e}')
        return False, str(e)


def switch_provider(provider_key: str) -> tuple[bool, str]:
    provider = get_provider(provider_key)
    if not provider:
        return False, f'Unknown provider: {provider_key}'
    if provider.get('auth_mode') == 'chatgpt':
        return switch_to_native(provider_key)
    return switch_to_custom_provider(provider_key)
