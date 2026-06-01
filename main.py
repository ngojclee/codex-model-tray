"""
Codex Model Tray — System tray app for switching Codex models & providers.

Features:
- Right-click: Provider switch, model switch, fix data, stats
- Left-click: Quick model picker popup
- File watcher: auto-update tray on external config changes
"""

import os
import subprocess
import sys
import threading

from PIL import Image, ImageDraw, ImageFont
import pystray

import config_manager as cfg
import db_manager as db
import portable_settings as vault
from providers import get_models_for_provider, get_provider, list_providers, secrets_file_path
from ui.window_utils import set_app_window_icon

# ── Base dir (PyInstaller-compatible) ───────────────────────
if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(sys.executable)
    _RESOURCE_DIR = getattr(sys, '_MEIPASS', _BASE_DIR)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _RESOURCE_DIR = _BASE_DIR

# ── Globals ─────────────────────────────────────────────────
_tray_icon: pystray.Icon | None = None
_watcher_thread: threading.Thread | None = None
_stop_watcher = threading.Event()
_STARTUP_REG_PATH = r'Software\Microsoft\Windows\CurrentVersion\Run'
_STARTUP_VALUE_NAME = 'CodexModelTray'
_SINGLE_INSTANCE_MUTEX = r'Local\CodexModelTray.SingleInstance'
_single_instance_mutex_handle = None
_single_instance_lock_file = None


def _ensure_single_instance() -> bool:
    """Return False when another real tray instance is already running."""
    if sys.platform != 'win32':
        return True

    import ctypes
    import msvcrt

    global _single_instance_mutex_handle, _single_instance_lock_file

    lock_path = os.path.join(_BASE_DIR, 'CodexModelTray.lock')
    try:
        lock_file = open(lock_path, 'a+b')
        lock_file.seek(0)
        lock_file.write(b'0')
        lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        _single_instance_lock_file = lock_file
    except OSError:
        try:
            lock_file.close()
        except Exception:
            pass
        return False

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    handle = kernel32.CreateMutexW(None, True, _SINGLE_INSTANCE_MUTEX)
    if not handle:
        return True

    already_exists = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    if already_exists:
        kernel32.CloseHandle(handle)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            lock_file.close()
        except Exception:
            pass
        _single_instance_lock_file = None
        return False

    _single_instance_mutex_handle = handle
    return True


# ── Icon loading ────────────────────────────────────────────

def _load_ico() -> Image.Image | None:
    """Load the bundled icon.ico file."""
    # Priority: raw PNG if available (better transparency), then ICO
    png_path = os.path.join(_RESOURCE_DIR, 'assets', 'ico.png')
    ico_path = os.path.join(_RESOURCE_DIR, 'assets', 'icon.ico')

    try:
        if os.path.exists(png_path):
            return Image.open(png_path).convert('RGBA').resize((64, 64), Image.Resampling.LANCZOS)
        return Image.open(ico_path).convert('RGBA').resize((64, 64), Image.Resampling.LANCZOS)
    except Exception:
        return None


def _create_icon_image(text: str = 'CX', bg_color: str = '#1E88E5') -> Image.Image:
    """Generate a 64x64 tray icon with text (fallback)."""
    size = 64
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Circle background
    draw.ellipse([2, 2, size - 2, size - 2], fill=bg_color)

    # Text
    try:
        font = ImageFont.truetype('segoeui.ttf', 22)
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2
    y = (size - th) // 2 - 2
    draw.text((x, y), text, fill='white', font=font)

    return img


def _get_icon_image() -> Image.Image:
    """Get tray icon: use bundled .ico or generate fallback."""
    vault.try_unlock_with_trusted_machine()
    status = vault.vault_status()
    if status.get('state') in {'error', 'missing'}:
        return _create_icon_image('!', '#B91C1C')
    if status.get('state') == 'locked':
        return _create_icon_image('LK', '#B91C1C')

    ico = _load_ico()
    if ico:
        return ico
    provider_key = cfg.get_current_provider()
    provider = get_provider(provider_key) or {}
    colors = {
        'cliproxy': '#00897B',
        'krouter': '#F57C00',
        'native': '#7B1FA2',
    }
    labels = {
        'cliproxy': 'CP',
        'krouter': 'KR',
        'native': 'OA',
    }
    label = labels.get(provider_key) or provider.get('id', provider_key).upper()[:2] or 'CX'
    return _create_icon_image(
        label,
        colors.get(provider_key, '#1E88E5'),
    )


# ── Tray tooltip ────────────────────────────────────────────

def _get_tooltip() -> str:
    vault.try_unlock_with_trusted_machine()
    model = cfg.get_current_model()
    provider_key = cfg.get_current_provider()
    provider = get_provider(provider_key) or {}
    label = provider.get('label', provider_key)
    status = vault.vault_status()
    return f'Codex: {model}\nProvider: {label}\nVault: {status["state"]}'


def _startup_supported() -> bool:
    return sys.platform == 'win32'


def _startup_command() -> str:
    if getattr(sys, 'frozen', False):
        parts = [os.path.abspath(sys.executable)]
    else:
        python_exe = os.path.abspath(sys.executable)
        pythonw_exe = os.path.join(os.path.dirname(python_exe), 'pythonw.exe')
        if os.path.exists(pythonw_exe):
            python_exe = pythonw_exe
        parts = [python_exe, os.path.abspath(__file__)]
    return subprocess.list2cmdline(parts)


def _read_startup_entry() -> str:
    if not _startup_supported():
        return ''

    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_PATH, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, _STARTUP_VALUE_NAME)
            return str(value).strip()
    except OSError:
        return ''


def _normalize_startup_command(command: str) -> str:
    return ' '.join(command.strip().split()).casefold()


def _start_with_windows_enabled() -> bool:
    command = _read_startup_entry()
    if not command:
        return False
    return _normalize_startup_command(command) == _normalize_startup_command(_startup_command())


def _start_with_windows_stale() -> bool:
    command = _read_startup_entry()
    if not command:
        return False
    return not _start_with_windows_enabled()


def _set_start_with_windows(enabled: bool) -> None:
    if not _startup_supported():
        raise RuntimeError('Start With Windows is only available on Windows.')

    import winreg

    if enabled:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_PATH) as key:
            winreg.SetValueEx(key, _STARTUP_VALUE_NAME, 0, winreg.REG_SZ, _startup_command())
        return

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _STARTUP_VALUE_NAME)
    except FileNotFoundError:
        return


def _start_with_windows_label() -> str:
    if not _startup_supported():
        return 'Start With Windows (Unsupported)'
    if _start_with_windows_stale():
        return 'Start With Windows (Update Path)'
    return 'Start With Windows'


# ── Menu actions ────────────────────────────────────────────

def _on_switch_model(icon: pystray.Icon):
    """Open model picker popup in a separate thread."""
    def _run():
        from ui.model_picker import show_model_picker
        provider = cfg.get_current_provider()
        current = cfg.get_current_model()

        def on_select(model_id):
            if model_id and model_id != current:
                cfg.set_model(model_id)
                _update_tray(icon)
                icon.notify(f'Switched to {model_id}', 'Codex Model Tray')

        show_model_picker(provider, current, on_select)

    threading.Thread(target=_run, daemon=True).start()


def _on_switch_provider(icon: pystray.Icon, provider_key: str):
    """Switch to a specific provider."""
    current = cfg.get_current_provider()
    provider = get_provider(provider_key) or {}
    is_refresh = provider_key == current

    success, message = cfg.switch_provider(provider_key)

    if success:
        # Also fix all threads
        results = db.fix_all(provider_key)
        _update_tray(icon)
        label = provider.get('label', provider_key)
        action = 'Refreshed' if is_refresh else 'Switched to'
        icon.notify(
            f'{action} {label}\n'
            f'Provider synced: {results["provider_fixed"]} conversations '
            f'({results["vscode_provider_fixed"]} mismatched)\n'
            f'Sessions synced: {results["session_meta_fixed"]} lines '
            f'in {results["session_meta_files_fixed"]} files\n'
            f'Restart Codex if old chat lists do not refresh immediately.',
            'Codex Model Tray',
        )
    else:
        _update_tray(icon)
        icon.notify(f'Failed to switch provider:\n{message}', 'Codex Model Tray')


def _sync_current_provider_config() -> None:
    provider_key = cfg.get_current_provider()
    provider = get_provider(provider_key)
    if not provider or provider.get('auth_mode') == 'chatgpt':
        return
    success, message = cfg.switch_provider(provider_key)
    if not success:
        print(f'[Codex Model Tray] Provider config refresh skipped: {message}')


def _prompt_password(
    title: str,
    primary_label: str,
    message: str,
    confirm: bool = False,
    current: bool = False,
) -> tuple[str, str] | tuple[str, str, str] | None:
    import customtkinter as ctk

    result: dict[str, str] | None = None
    height = 260 if confirm or current else 210
    if current and confirm:
        height = 330

    root = ctk.CTk()
    root.title(title)
    set_app_window_icon(root)
    root.geometry(f'500x{height}')
    root.attributes('-topmost', True)
    root.resizable(False, False)
    ctk.set_appearance_mode('dark')

    root.update_idletasks()
    x = (root.winfo_screenwidth() - 500) // 2
    y = (root.winfo_screenheight() - height) // 2
    root.geometry(f'500x{height}+{x}+{y}')

    shell = ctk.CTkFrame(root, fg_color='#141923')
    shell.pack(fill='both', expand=True, padx=14, pady=14)
    ctk.CTkLabel(
        shell,
        text=title,
        font=ctk.CTkFont(size=18, weight='bold'),
    ).pack(anchor='w', padx=16, pady=(14, 4))
    ctk.CTkLabel(
        shell,
        text=message,
        text_color='#AAB4C3',
        wraplength=440,
        justify='left',
    ).pack(anchor='w', padx=16, pady=(0, 12))

    current_var = ctk.StringVar()
    password_var = ctk.StringVar()
    confirm_var = ctk.StringVar()

    first_entry = None
    if current:
        ctk.CTkLabel(shell, text='Current password', anchor='w').pack(fill='x', padx=16)
        first_entry = ctk.CTkEntry(shell, textvariable=current_var, show='*', height=34)
        first_entry.pack(fill='x', padx=16, pady=(4, 10))

    ctk.CTkLabel(shell, text=primary_label, anchor='w').pack(fill='x', padx=16)
    password_entry = ctk.CTkEntry(shell, textvariable=password_var, show='*', height=34)
    password_entry.pack(fill='x', padx=16, pady=(4, 10))
    first_entry = first_entry or password_entry

    if confirm:
        ctk.CTkLabel(shell, text='Confirm password', anchor='w').pack(fill='x', padx=16)
        ctk.CTkEntry(shell, textvariable=confirm_var, show='*', height=34).pack(fill='x', padx=16, pady=(4, 10))

    status_label = ctk.CTkLabel(shell, text='', text_color='#FCA5A5')
    status_label.pack(anchor='w', padx=16, pady=(0, 6))

    actions = ctk.CTkFrame(shell, fg_color='transparent')
    actions.pack(fill='x', padx=16, pady=(0, 14))

    def _finish():
        nonlocal result
        password = password_var.get()
        confirmation = confirm_var.get()
        if not password:
            status_label.configure(text='Password is required.')
            return
        if confirm and password != confirmation:
            status_label.configure(text='Passwords do not match.')
            return
        if current and not current_var.get():
            status_label.configure(text='Current password is required.')
            return
        result = {
            'current': current_var.get(),
            'password': password,
            'confirm': confirmation,
        }
        root.quit()
        root.destroy()

    def _cancel():
        root.quit()
        root.destroy()

    ctk.CTkButton(
        actions,
        text='OK',
        command=_finish,
        fg_color='#2563EB',
        hover_color='#1D4ED8',
        width=110,
    ).pack(side='left', padx=(0, 8))
    ctk.CTkButton(
        actions,
        text='Cancel',
        command=_cancel,
        fg_color='#475569',
        hover_color='#334155',
        width=110,
    ).pack(side='left')

    first_entry.focus_set()
    root.bind('<Return>', lambda _event: _finish())
    root.bind('<Escape>', lambda _event: _cancel())
    root.protocol('WM_DELETE_WINDOW', _cancel)
    root.mainloop()

    if result is None:
        return None
    if current and confirm:
        return result['current'], result['password'], result['confirm']
    if confirm:
        return result['password'], result['confirm']
    return result['password'], ''


def _on_create_vault(icon: pystray.Icon):
    def _run():
        values = _prompt_password(
            'Create Secrets Vault',
            'New vault password',
            'This password encrypts API keys and native auth. Trust this machine after creation for one-time entry on this PC.',
            confirm=True,
        )
        if not values:
            return
        password, _confirmation = values
        try:
            vault.create_vault(password, trust_machine=True)
            _update_tray(icon)
            icon.notify('Secrets vault created and this machine is trusted.', 'Codex Model Tray')
        except Exception as exc:
            _update_tray(icon)
            icon.notify(f'Could not create secrets vault:\n{exc}', 'Codex Model Tray')

    threading.Thread(target=_run, daemon=True).start()


def _on_unlock_vault(icon: pystray.Icon):
    def _run():
        values = _prompt_password(
            'Unlock Secrets Vault',
            'Vault password',
            'Unlock once, then trust this machine so future launches open the vault automatically here.',
        )
        if not values:
            return
        password, _empty = values
        try:
            vault.unlock_vault(password, trust_machine=True)
            _update_tray(icon)
            icon.notify('Secrets vault unlocked and this machine is trusted.', 'Codex Model Tray')
        except Exception as exc:
            _update_tray(icon)
            icon.notify(f'Wrong password or invalid vault:\n{exc}', 'Codex Model Tray')

    threading.Thread(target=_run, daemon=True).start()


def _on_change_vault_password(icon: pystray.Icon):
    def _run():
        values = _prompt_password(
            'Change Vault Password',
            'New password',
            'Current password must unlock the vault before it can be re-encrypted.',
            confirm=True,
            current=True,
        )
        if not values:
            return
        current_password, new_password, _confirmation = values
        try:
            vault.change_vault_password(current_password, new_password, trust_machine=True)
            _update_tray(icon)
            icon.notify('Vault password changed and this machine remains trusted.', 'Codex Model Tray')
        except Exception as exc:
            _update_tray(icon)
            icon.notify(f'Could not change vault password:\n{exc}', 'Codex Model Tray')

    threading.Thread(target=_run, daemon=True).start()


def _on_backup_native_auth(icon: pystray.Icon):
    ok, message = cfg.backup_native_auth()
    _update_tray(icon)
    icon.notify(message, 'Codex Model Tray')


def _on_trust_machine(icon: pystray.Icon):
    try:
        if not (vault.is_vault_unlocked() or vault.try_unlock_with_trusted_machine()):
            _on_unlock_vault(icon)
            return
        vault.trust_this_machine()
        _update_tray(icon)
        icon.notify('This machine is trusted for the current vault.', 'Codex Model Tray')
    except Exception as exc:
        _update_tray(icon)
        icon.notify(f'Could not trust this machine:\n{exc}', 'Codex Model Tray')


def _on_lock_vault(icon: pystray.Icon):
    vault.lock_vault()
    _update_tray(icon)
    icon.notify('Secrets vault locked for this app session.', 'Codex Model Tray')


def _on_toggle_start_with_windows(icon: pystray.Icon, _item=None):
    if not _startup_supported():
        icon.notify('Start With Windows is only available on Windows.', 'Codex Model Tray')
        return

    try:
        if _start_with_windows_stale():
            _set_start_with_windows(True)
            message = 'Start With Windows updated for the current app location.'
        elif _start_with_windows_enabled():
            _set_start_with_windows(False)
            message = 'Start With Windows disabled.'
        else:
            _set_start_with_windows(True)
            message = 'Start With Windows enabled.'
        _update_tray(icon)
        icon.notify(message, 'Codex Model Tray')
    except Exception as exc:
        _update_tray(icon)
        icon.notify(f'Could not update Start With Windows:\n{exc}', 'Codex Model Tray')


def _on_fix_data(icon: pystray.Icon):
    """Fix conversation provider & effort only (safe — does not touch model names)."""
    provider = cfg.get_current_provider()
    results = db.fix_all(provider)
    icon.notify(
        f'Fix complete!\n'
        f'Provider synced: {results["provider_fixed"]} conversations '
        f'({results["vscode_provider_fixed"]} mismatched)\n'
        f'Sessions synced: {results["session_meta_fixed"]} lines '
        f'in {results["session_meta_files_fixed"]} files\n'
        f'Effort cleared: {results["effort_fixed"]} threads\n'
        f'DB provider key: {results["db_provider"]}',
        'Codex Model Tray',
    )


def _on_force_sync_models(icon: pystray.Icon):
    """Force all Codex Desktop conversations to use the top-level current model."""
    model = cfg.get_current_model()
    results = db.fix_all_threads_to_model(model)
    icon.notify(
        f'Force synced {results["updated"]} conversations to {model}\n'
        f'Active: {results["active_updated"]}, archived: {results["archived_updated"]}\n'
        f'Sessions: {results["session_model_fixed"]} lines',
        'Codex Model Tray',
    )


def _on_show_stats(icon: pystray.Icon):
    """Show thread stats as notification."""
    stats = db.get_thread_stats()
    total = db.get_total_threads()
    visible_total = db.get_visible_vscode_threads()
    visible_provider_stats = db.get_visible_vscode_provider_stats()
    lines = [
        f'All rows: {total}',
        f'Visible VS Code chats: {visible_total}',
        '',
    ]
    for provider, count in visible_provider_stats[:4]:
        lines.append(f'VS Code {provider}: {count}')
    lines.append('')
    for model, provider, count in stats[:5]:
        lines.append(f'{model} ({provider}): {count}')
    icon.notify('\n'.join(lines), 'Codex Model Tray — Stats')


def _on_open_config(_icon: pystray.Icon):
    """Open config.toml in default editor."""
    cfg.ensure_config_file()
    os.startfile(cfg.config_path())


def _on_open_auth(_icon: pystray.Icon):
    """Open auth.json in default editor."""
    if not os.path.exists(cfg.auth_path()):
        _icon.notify('auth.json does not exist yet. Sign in with Codex or switch provider first.', 'Codex Model Tray')
        return
    os.startfile(cfg.auth_path())


def _on_manage_providers(icon: pystray.Icon):
    """Open the custom provider manager."""
    def _run():
        from ui.provider_manager import show_provider_manager

        def _changed():
            if _tray_icon:
                _update_tray(_tray_icon)

        show_provider_manager(on_changed=_changed)
        if _tray_icon:
            _update_tray(_tray_icon)

    threading.Thread(target=_run, daemon=True).start()


def _on_fix_missing_projects(icon: pystray.Icon):
    """Scan DB threads and merge missing projects into Codex UI."""
    success, total_count, added_count = cfg.fix_missing_projects()
    if success:
        if added_count > 0:
            icon.notify(
                f'Found and added {added_count} missing projects!\nRestart Codex Desktop to see them.',
                'Codex Model Tray',
            )
        else:
            icon.notify(
                f'All {total_count} projects are already in the UI.\nNo changes needed.',
                'Codex Model Tray',
            )
    else:
        icon.notify('Failed to extract or save projects.', 'Codex Model Tray')


def _on_change_api_key(icon: pystray.Icon):
    """Open a dialog to change the API key for the current provider."""
    def _run():
        import customtkinter as ctk
        if not (vault.is_vault_unlocked() or vault.try_unlock_with_trusted_machine()):
            icon.notify('Unlock or create the secrets vault before saving API keys.', 'Codex Model Tray')
            _update_tray(icon)
            return

        provider_key = cfg.get_current_provider()
        provider = get_provider(provider_key) or {}
        if provider.get('auth_mode') == 'chatgpt':
            icon.notify('This provider uses account login, no API key needed.', 'Codex')
            return

        # auth.json is managed by Codex and cannot be relied on as the long-term source.
        current_key = cfg.get_effective_api_key(provider_key)

        root = ctk.CTk()
        root.title(f'🔑 API Key — {provider_key}')
        set_app_window_icon(root)
        root.geometry('520x220')
        root.attributes('-topmost', True)
        root.resizable(False, False)
        ctk.set_appearance_mode('dark')

        # Center
        root.update_idletasks()
        x = (root.winfo_screenwidth() - 520) // 2
        y = (root.winfo_screenheight() - 220) // 2
        root.geometry(f'520x220+{x}+{y}')

        ctk.CTkLabel(root, text=f'API Key for {provider_key}',
                      font=ctk.CTkFont(size=16, weight='bold')).pack(pady=(12, 2))
        ctk.CTkLabel(root, text=f'Saved to the vault and auth.json.\n{secrets_file_path()}',
                      font=ctk.CTkFont(size=11), text_color='#888').pack(pady=(0, 4))

        key_var = ctk.StringVar(value=current_key)
        entry = ctk.CTkEntry(root, textvariable=key_var, width=480,
                              font=ctk.CTkFont(size=13))
        entry.pack(padx=16, pady=8)
        entry.focus_set()
        entry.select_range(0, 'end')

        def _save():
            new_key = key_var.get().strip()
            if new_key:
                try:
                    # Save to portable secrets for future provider switches.
                    cfg.set_custom_api_key(provider_key, new_key)
                    # Update auth.json now as best-effort; Codex may rewrite it later.
                    cfg._write_auth({'auth_mode': 'apikey', 'OPENAI_API_KEY': new_key})
                    _update_tray(icon)
                    icon.notify(
                        f'API key saved for {provider_key}\n'
                        f'Vault and auth.json updated now. config.toml updates on next provider switch.',
                        'Codex Model Tray',
                    )
                except Exception as exc:
                    _update_tray(icon)
                    icon.notify(f'Could not save API key:\n{exc}', 'Codex Model Tray')
            root.quit()
            root.destroy()

        ctk.CTkButton(root, text='💾 Save', command=_save,
                       font=ctk.CTkFont(size=14), height=36).pack(pady=8)

        root.bind('<Return>', lambda e: _save())
        root.bind('<Escape>', lambda e: (root.quit(), root.destroy()))
        root.protocol('WM_DELETE_WINDOW', lambda: (root.quit(), root.destroy()))
        root.mainloop()

    threading.Thread(target=_run, daemon=True).start()


def _on_quit(icon: pystray.Icon):
    """Quit the tray app."""
    _stop_watcher.set()
    icon.stop()


# ── Tray update ─────────────────────────────────────────────

def _update_tray(icon: pystray.Icon):
    """Update tray icon and tooltip."""
    icon.icon = _get_icon_image()
    icon.title = _get_tooltip()
    icon.menu = _build_menu()


# ── Build menu ──────────────────────────────────────────────

def _build_menu() -> pystray.Menu:
    """Build the right-click context menu."""
    vault.try_unlock_with_trusted_machine()
    vault_info = vault.vault_status()

    def _vault_status_text() -> str:
        state = str(vault_info.get('state', 'locked'))
        labels = {
            'unlocked': 'OK Vault: Unlocked',
            'locked': 'ALERT Vault: Locked',
            'missing': 'ALERT Vault: Not Created',
            'error': 'ALERT Vault: Password Needed',
        }
        return labels.get(state, f'ALERT Vault: {state}')

    def _vault_items():
        state = str(vault_info.get('state', 'locked'))
        items = [
            pystray.MenuItem(
                lambda _: str(vault.vault_status().get('message', 'Secrets vault')),
                None,
                enabled=False,
            ),
        ]
        if state == 'missing':
            items.append(pystray.MenuItem('Create Secrets Vault...', _on_create_vault))
        else:
            items.append(pystray.MenuItem('Unlock / Re-enter Password...', _on_unlock_vault))
            items.append(pystray.MenuItem('Change Vault Password...', _on_change_vault_password))
            items.append(pystray.MenuItem('Trust This Machine', _on_trust_machine))
            items.append(pystray.MenuItem('Lock For This Session', _on_lock_vault))
        items.append(pystray.MenuItem('Back Up Native Auth Now', _on_backup_native_auth))
        return items

    def _provider_items():
        current = cfg.get_current_provider()
        items = []
        for key, info in list_providers():
            check = '✅ ' if key == current else ''

            def _make_provider_handler(k):
                def handler(icon, item):
                    _on_switch_provider(icon, k)
                return handler

            items.append(
                pystray.MenuItem(
                    f'{check}{info["icon"]} {info["label"]}',
                    _make_provider_handler(key),
                )
            )
        return items

    # Quick model items for right-click (from pinned models)
    def _quick_model_items():
        provider = cfg.get_current_provider()
        current = cfg.get_current_model()
        all_models = {model[0]: model for model in get_models_for_provider(provider)}
        pinned = cfg.get_pinned_models()
        models = [all_models[model_id] for model_id in pinned if model_id in all_models]

        items = []
        for model_id, desc, icon_char in models:
            check = '✅ ' if model_id == current else ''

            def _make_model_handler(mid):
                def handler(icon, item):
                    cfg.set_model(mid)
                    _update_tray(icon)
                    icon.notify(f'Switched to {mid}', 'Codex Model Tray')
                return handler

            items.append(
                pystray.MenuItem(
                    f'{check}{icon_char} {model_id}',
                    _make_model_handler(model_id),
                )
            )

        if not items:
            items.append(pystray.MenuItem('No pinned models', None, enabled=False))

        return items


    # Quick model items for Subagent
    def _quick_subagent_items():
        sub_model = cfg.get_subagent_model()
        provider = cfg.get_current_provider()
        all_models = {model[0]: model for model in get_models_for_provider(provider)}
        pinned = cfg.get_pinned_models()
        models = [all_models[model_id] for model_id in pinned if model_id in all_models]

        items = []
        for model_id, desc, icon_char in models:
            check = '✅ ' if model_id == sub_model else ''

            def _make_subagent_handler(mid):
                def handler(icon, item):
                    cfg.add_or_update_subagent(mid)
                    _update_tray(icon)
                    icon.notify(f'Subagent changed to {mid}', 'Codex')
                return handler

            items.append(
                pystray.MenuItem(
                    f'{check}{icon_char} {model_id}',
                    _make_subagent_handler(model_id),
                )
            )

        if not items:
            items.append(pystray.MenuItem('No pinned models', None, enabled=False))

        return items

    def _on_switch_subagent(icon: pystray.Icon):
        def _run():
            from ui.model_picker import show_model_picker
            provider = cfg.get_current_provider()
            current = cfg.get_subagent_model() or ''

            def on_select(mid):
                if mid and mid != current:
                    cfg.add_or_update_subagent(mid)
                    _update_tray(icon)
                    icon.notify(f'Subagent changed to {mid}', 'Codex')

            show_model_picker(provider, current, on_select)

        threading.Thread(target=_run, daemon=True).start()

    def _on_remove_subagent(icon: pystray.Icon):
        cfg.remove_subagent()
        _update_tray(icon)
        icon.notify('Subagent config removed', 'Codex')

    def _on_add_subagent(icon: pystray.Icon):
        main_mod = cfg.get_current_model()
        cfg.add_or_update_subagent(main_mod)
        _update_tray(icon)
        icon.notify(f'Subagent created with {main_mod}', 'Codex')

    sub_model = cfg.get_subagent_model()
    sub_text = f'🤖 Subagent: {sub_model}' if sub_model else '🤖 Subagent: Disabled'

    return pystray.Menu(
        # Status header
        pystray.MenuItem(
            lambda _: f'🚀 Main: {cfg.get_current_model()}',
            None,
            enabled=False,
        ),
        pystray.MenuItem(
            lambda _: sub_text,
            None,
            enabled=False,
        ),
        pystray.MenuItem(
            lambda _: _vault_status_text(),
            None,
            enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            'Secrets Vault',
            pystray.Menu(*_vault_items()),
        ),
        pystray.Menu.SEPARATOR,

        # Provider submenu
        pystray.MenuItem(
            '▶ Switch Provider',
            pystray.Menu(*_provider_items()),
        ),
        pystray.MenuItem(
            '🧩 Manage Custom Providers...',
            _on_manage_providers,
        ),
        pystray.Menu.SEPARATOR,

        # Main models
        pystray.MenuItem(
            '▶ Quick Main Models',
            pystray.Menu(*_quick_model_items()),
        ),
        pystray.MenuItem(
            '🔍 All Main Models...',
            _on_switch_model,
        ),
        pystray.Menu.SEPARATOR,

        # Subagent
        pystray.MenuItem(
            '▶ Quick Subagents',
            pystray.Menu(*_quick_subagent_items()),
        ),
        pystray.MenuItem(
            '🔍 All Subagents...',
            _on_switch_subagent,
        ),
        pystray.MenuItem(
            '❌ Remove Subagent',
            _on_remove_subagent,
        ) if sub_model else pystray.MenuItem(
            '➕ Add Subagent',
            _on_add_subagent,
        ),
        pystray.Menu.SEPARATOR,

        # Fix & Stats
        pystray.MenuItem('🔧 Fix Threads (Safe)', _on_fix_data),
        pystray.MenuItem('⚠️ Force Sync All Models', _on_force_sync_models),
        pystray.MenuItem('📊 Thread Stats', _on_show_stats),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('📁 Fix Missing Projects', _on_fix_missing_projects),
        pystray.MenuItem('🔑 Change API Key', _on_change_api_key),
        pystray.MenuItem(
            lambda _: _start_with_windows_label(),
            _on_toggle_start_with_windows,
            checked=lambda _: _start_with_windows_enabled(),
            enabled=lambda _: _startup_supported(),
        ),
        pystray.MenuItem('⚙️ Open config.toml', _on_open_config),
        pystray.MenuItem('🔐 Open auth.json', _on_open_auth),
        pystray.MenuItem('❌ Quit', _on_quit),
    )


# ── File watcher ────────────────────────────────────────────

def _file_mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _watch_settings_files():
    """Watch Codex config/auth files for external changes and update tray."""
    watched = {
        'config': cfg.config_path(),
        'auth': cfg.auth_path(),
    }
    last_mtimes = {name: _file_mtime(path) for name, path in watched.items()}

    while not _stop_watcher.is_set():
        changed = []
        for name, path in watched.items():
            mtime = _file_mtime(path)
            if mtime != last_mtimes.get(name):
                last_mtimes[name] = mtime
                changed.append((name, path))

        if changed and _tray_icon:
            try:
                for name, path in changed:
                    cfg.audit_observed_file_change(name, path)
                _update_tray(_tray_icon)
            except Exception:
                pass
        _stop_watcher.wait(2)  # Poll every 2 seconds


# ── Entry point ─────────────────────────────────────────────

def main():
    global _tray_icon, _watcher_thread

    if not _ensure_single_instance():
        print('[Codex Model Tray] Another instance is already running; exiting.')
        return

    vault.try_unlock_with_trusted_machine()
    icon_image = _get_icon_image()

    _tray_icon = pystray.Icon(
        name='codex-model-tray',
        icon=icon_image,
        title=_get_tooltip(),
        menu=_build_menu(),
    )

    # Start file watcher
    _watcher_thread = threading.Thread(target=_watch_settings_files, daemon=True)
    _watcher_thread.start()

    # Left-click → open model picker
    # Note: pystray on Windows fires on_activate for double-click
    # We use the menu for primary interaction instead

    print('[Codex Model Tray] Running... Right-click tray icon for menu.')
    if not vault.vault_exists():
        threading.Timer(0.8, lambda: _on_create_vault(_tray_icon)).start()
    _tray_icon.run()


if __name__ == '__main__':
    main()
