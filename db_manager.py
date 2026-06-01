"""
Database manager for Codex state_5.sqlite thread operations.

Handles:
- Thread model migration
- reasoning_effort cleanup for API-provider visible chats
- Thread statistics
"""

import json
import os
import sqlite3
from pathlib import Path
from providers import get_db_provider_key, get_provider


def _codex_home() -> str:
    return os.environ.get('CODEX_HOME', os.path.join(os.path.expanduser('~'), '.codex'))


def _db_path() -> str:
    return os.path.join(_codex_home(), 'state_5.sqlite')


def _sessions_path() -> Path:
    return Path(_codex_home()) / 'sessions'


def _connect():
    return sqlite3.connect(_db_path())


def _build_where(*, source: str | None = None, archived: int | None = None) -> tuple[str, list]:
    clauses = []
    params: list = []
    if source is not None:
        clauses.append('source = ?')
        params.append(source)
    if archived is not None:
        clauses.append('archived = ?')
        params.append(archived)
    return (' WHERE ' + ' AND '.join(clauses)) if clauses else '', params


def _count_threads(*, source: str | None = None, archived: int | None = None) -> int:
    conn = _connect()
    try:
        where_sql, params = _build_where(source=source, archived=archived)
        query = f'SELECT COUNT(*) FROM threads{where_sql}'
        return conn.execute(query, params).fetchone()[0]
    finally:
        conn.close()


def _count_provider_mismatches(provider_key: str, *, source: str | None = None, archived: int | None = None) -> int:
    target_provider = get_db_provider_key(provider_key)
    conn = _connect()
    try:
        where_sql, params = _build_where(source=source, archived=archived)
        prefix = ' WHERE ' if not where_sql else f'{where_sql} AND '
        query = (
            'SELECT COUNT(*) FROM threads'
            f'{prefix}COALESCE(model_provider, "") != ?'
        )
        return conn.execute(query, [*params, target_provider]).fetchone()[0]
    finally:
        conn.close()


# ── Stats ───────────────────────────────────────────────────

def get_thread_stats() -> list[tuple[str, str, int]]:
    """Return [(model, provider, count), ...] sorted by count desc."""
    conn = _connect()
    try:
        rows = conn.execute(
            'SELECT model, model_provider, COUNT(*) as cnt '
            'FROM threads GROUP BY model, model_provider '
            'ORDER BY cnt DESC'
        ).fetchall()
        return rows
    finally:
        conn.close()


def get_visible_vscode_provider_stats() -> list[tuple[str, int]]:
    """Return provider counts for chat rows that VS Code actually shows."""
    conn = _connect()
    try:
        rows = conn.execute(
            'SELECT COALESCE(model_provider, "<null>"), COUNT(*) as cnt '
            'FROM threads WHERE source = ? AND archived = 0 '
            'GROUP BY model_provider ORDER BY cnt DESC',
            ('vscode',),
        ).fetchall()
        return rows
    finally:
        conn.close()


def get_total_threads() -> int:
    return _count_threads()


def get_visible_vscode_threads() -> int:
    return _count_threads(source='vscode', archived=0)


# ── Fix operations ──────────────────────────────────────────

# Scope of Codex Desktop conversations. Subagent threads (source is JSON blob
# like {"subagent":...}), exec runs, and CLI runs are intentionally left alone —
# their provider tracks the runtime that spawned them.
_VSCODE_FILTER = "source = 'vscode' AND archived = 0"
_VSCODE_CONVERSATION_FILTER = "source = 'vscode'"


def fix_threads_provider(provider_key: str) -> int:
    """Update all Codex Desktop conversation threads to use the given provider."""
    target_provider = get_db_provider_key(provider_key)
    conn = _connect()
    try:
        cur = conn.execute(
            f'UPDATE threads SET model_provider = ? '
            f'WHERE {_VSCODE_CONVERSATION_FILTER} AND COALESCE(model_provider, "") != ?',
            (target_provider, target_provider),
        )
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def _visible_vscode_thread_ids() -> set[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            f'SELECT id FROM threads WHERE {_VSCODE_FILTER}'
        ).fetchall()
        return {row[0] for row in rows if row[0]}
    finally:
        conn.close()

def _vscode_conversation_thread_ids() -> set[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            f'SELECT id FROM threads WHERE {_VSCODE_CONVERSATION_FILTER}'
        ).fetchall()
        return {row[0] for row in rows if row[0]}
    finally:
        conn.close()


def fix_session_provider_metadata(provider_key: str) -> dict:
    """Update session_meta provider values for Codex Desktop conversations."""
    target_provider = get_db_provider_key(provider_key)
    conversation_ids = _vscode_conversation_thread_ids()
    sessions_path = _sessions_path()
    results = {
        'session_meta_files_scanned': 0,
        'session_meta_files_fixed': 0,
        'session_meta_lines_seen': 0,
        'session_meta_fixed': 0,
        'session_meta_thread_ids_found': 0,
    }

    if not conversation_ids or not sessions_path.exists():
        return results

    seen_ids: set[str] = set()

    for path in sessions_path.rglob('*.jsonl'):
        results['session_meta_files_scanned'] += 1
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines(keepends=True)
        except Exception:
            continue

        changed = False
        new_lines: list[str] = []
        for line in lines:
            body = line.rstrip('\r\n')
            newline = line[len(body):]
            if 'session_meta' not in body:
                new_lines.append(line)
                continue

            try:
                obj = json.loads(body)
            except Exception:
                new_lines.append(line)
                continue

            payload = obj.get('payload') if isinstance(obj, dict) else None
            thread_id = payload.get('id') if isinstance(payload, dict) else None
            if obj.get('type') != 'session_meta' or thread_id not in conversation_ids:
                new_lines.append(line)
                continue

            results['session_meta_lines_seen'] += 1
            seen_ids.add(thread_id)
            if payload.get('model_provider') != target_provider:
                payload['model_provider'] = target_provider
                results['session_meta_fixed'] += 1
                changed = True
                new_lines.append(json.dumps(obj, ensure_ascii=False, separators=(',', ':')) + newline)
            else:
                new_lines.append(line)

        if changed:
            try:
                path.write_text(''.join(new_lines), encoding='utf-8', newline='')
                results['session_meta_files_fixed'] += 1
            except Exception:
                pass

    results['session_meta_thread_ids_found'] = len(seen_ids)
    return results

def fix_session_model_metadata(new_model: str) -> dict:
    """Update turn_context model values for Codex Desktop conversation files."""
    conversation_ids = _vscode_conversation_thread_ids()
    sessions_path = _sessions_path()
    results = {
        'session_model_files_scanned': 0,
        'session_model_files_fixed': 0,
        'session_model_lines_seen': 0,
        'session_model_fixed': 0,
        'session_model_thread_ids_found': 0,
    }

    if not conversation_ids or not sessions_path.exists():
        return results

    seen_ids: set[str] = set()

    for path in sessions_path.rglob('*.jsonl'):
        results['session_model_files_scanned'] += 1
        thread_id = next((item for item in conversation_ids if item in path.name), None)
        if not thread_id:
            continue

        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines(keepends=True)
        except Exception:
            continue

        seen_ids.add(thread_id)
        changed = False
        new_lines: list[str] = []
        for line in lines:
            body = line.rstrip('\r\n')
            newline = line[len(body):]
            if '"turn_context"' not in body or '"model"' not in body:
                new_lines.append(line)
                continue

            try:
                obj = json.loads(body)
            except Exception:
                new_lines.append(line)
                continue

            payload = obj.get('payload') if isinstance(obj, dict) else None
            if obj.get('type') != 'turn_context' or not isinstance(payload, dict):
                new_lines.append(line)
                continue

            results['session_model_lines_seen'] += 1
            line_changed = False
            if payload.get('model') != new_model:
                payload['model'] = new_model
                line_changed = True

            collaboration = payload.get('collaboration_mode')
            settings = collaboration.get('settings') if isinstance(collaboration, dict) else None
            if isinstance(settings, dict) and settings.get('model') != new_model:
                settings['model'] = new_model
                line_changed = True

            if line_changed:
                results['session_model_fixed'] += 1
                changed = True
                new_lines.append(json.dumps(obj, ensure_ascii=False, separators=(',', ':')) + newline)
            else:
                new_lines.append(line)

        if changed:
            try:
                path.write_text(''.join(new_lines), encoding='utf-8', newline='')
                results['session_model_files_fixed'] += 1
            except Exception:
                pass

    results['session_model_thread_ids_found'] = len(seen_ids)
    return results


def fix_threads_model(old_model: str, new_model: str) -> int:
    """Replace specific model in visible Codex Desktop threads."""
    conn = _connect()
    try:
        cur = conn.execute(
            f'UPDATE threads SET model = ? WHERE {_VSCODE_FILTER} AND model = ?',
            (new_model, old_model),
        )
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def fix_reasoning_effort(provider_key: str | None) -> int:
    """Clear reasoning_effort for visible chats when the active provider uses API keys."""
    provider = get_provider(provider_key)
    if not provider or provider.get('auth_mode') == 'chatgpt':
        return 0

    conn = _connect()
    try:
        cur = conn.execute(
            f'UPDATE threads SET reasoning_effort = NULL '
            f'WHERE {_VSCODE_FILTER} AND reasoning_effort IS NOT NULL'
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def fix_all_threads_to_model(new_model: str) -> dict:
    """Force all Codex Desktop conversation threads to use the target model."""
    session_results = fix_session_model_metadata(new_model)
    conn = _connect()
    try:
        before = conn.execute(
            f'SELECT archived, COUNT(*) FROM threads '
            f'WHERE {_VSCODE_CONVERSATION_FILTER} AND COALESCE(model, "") != ? '
            f'GROUP BY archived',
            (new_model,),
        ).fetchall()
        by_archived = {row[0]: row[1] for row in before}
        cur = conn.execute(
            f'UPDATE threads SET model = ? '
            f'WHERE {_VSCODE_CONVERSATION_FILTER} AND COALESCE(model, "") != ?',
            (new_model, new_model),
        )
        conn.commit()
        return {
            'model': new_model,
            'updated': cur.rowcount,
            'active_updated': by_archived.get(0, 0),
            'archived_updated': by_archived.get(1, 0),
            **session_results,
        }
    finally:
        conn.close()


def fix_all(provider_key: str) -> dict:
    """
    Run safe fixes only (does NOT change model to avoid breaking tool call history):
    1. Migrate all threads to current provider
    2. Clear reasoning_effort for API providers
    Returns summary dict.
    """
    session_results = fix_session_provider_metadata(provider_key)
    results = {
        'db_provider': get_db_provider_key(provider_key),
        'visible_vscode_total': get_visible_vscode_threads(),
        'vscode_conversation_total': _count_threads(source='vscode'),
        'vscode_provider_fixed': _count_provider_mismatches(provider_key, source='vscode'),
        'provider_fixed': fix_threads_provider(provider_key),
        'effort_fixed': fix_reasoning_effort(provider_key),
    }
    results.update(session_results)
    return results
