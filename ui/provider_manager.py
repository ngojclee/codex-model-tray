"""
Custom provider manager dialog for Codex Model Tray.
"""

from __future__ import annotations

import customtkinter as ctk

import portable_settings as vault
from ui.window_utils import set_app_window_icon
from providers import (
    get_custom_providers,
    remove_custom_provider,
    upsert_custom_provider,
)


class ProviderManagerApp(ctk.CTk):

    def __init__(self, on_changed: callable | None = None):
        super().__init__()
        self.on_changed = on_changed
        self.selected_provider_id: str | None = None
        self.provider_buttons: list[ctk.CTkButton] = []

        self.title('Custom Providers')
        set_app_window_icon(self)
        self.geometry('840x410')
        self.resizable(False, False)
        self.attributes('-topmost', True)
        ctk.set_appearance_mode('dark')

        self.update_idletasks()
        x = (self.winfo_screenwidth() - 840) // 2
        y = (self.winfo_screenheight() - 410) // 2
        self.geometry(f'840x410+{x}+{y}')

        shell = ctk.CTkFrame(self, fg_color='#131826')
        shell.pack(fill='both', expand=True, padx=12, pady=12)

        left = ctk.CTkFrame(shell, width=230, fg_color='#182033')
        left.pack(side='left', fill='y', padx=(0, 10), pady=0)
        left.pack_propagate(False)

        ctk.CTkLabel(
            left,
            text='Custom API Providers',
            font=ctk.CTkFont(size=16, weight='bold'),
        ).pack(anchor='w', padx=12, pady=(12, 6))

        ctk.CTkLabel(
            left,
            text='Custom providers store route data in the catalog.\nAPI keys require the unlocked secrets vault.\nModels are managed separately.',
            justify='left',
            text_color='#9aa4b2',
            font=ctk.CTkFont(size=11),
        ).pack(anchor='w', padx=12, pady=(0, 10))

        self.provider_list = ctk.CTkScrollableFrame(left, fg_color='#111827')
        self.provider_list.pack(fill='both', expand=True, padx=10, pady=(0, 10))

        ctk.CTkButton(
            left,
            text='New Provider',
            command=self._new_provider,
            fg_color='#2563eb',
            hover_color='#1d4ed8',
        ).pack(fill='x', padx=10, pady=(0, 12))

        right = ctk.CTkFrame(shell, fg_color='#101726')
        right.pack(side='left', fill='both', expand=True)

        ctk.CTkLabel(
            right,
            text='Provider Details',
            font=ctk.CTkFont(size=17, weight='bold'),
        ).pack(anchor='w', padx=16, pady=(14, 10))

        form = ctk.CTkFrame(right, fg_color='transparent')
        form.pack(fill='both', expand=True, padx=16)

        self.name_var = ctk.StringVar()
        self.id_var = ctk.StringVar()
        self.base_url_var = ctk.StringVar()
        self.api_key_var = ctk.StringVar()

        self._field(form, 'Name', self.name_var, 'My Provider')
        self._field(form, 'ID', self.id_var, 'my-provider')
        self._field(form, 'Base URL', self.base_url_var, 'https://example.com/v1')
        self._field(form, 'API Key', self.api_key_var, 'sk-...')

        self.status_label = ctk.CTkLabel(
            right,
            text='',
            text_color='#cbd5e1',
            wraplength=540,
            justify='left',
        )
        self.status_label.pack(anchor='w', padx=16, pady=(6, 8))

        actions = ctk.CTkFrame(right, fg_color='transparent')
        actions.pack(fill='x', padx=16, pady=(0, 16))

        ctk.CTkButton(
            actions,
            text='Save',
            command=self._save,
            fg_color='#15803d',
            hover_color='#166534',
            width=110,
        ).pack(side='left', padx=(0, 8))
        ctk.CTkButton(
            actions,
            text='Delete',
            command=self._delete,
            fg_color='#b91c1c',
            hover_color='#991b1b',
            width=110,
        ).pack(side='left', padx=(0, 8))
        ctk.CTkButton(
            actions,
            text='Close',
            command=self._close,
            fg_color='#475569',
            hover_color='#334155',
            width=110,
        ).pack(side='left')

        self.bind('<Control-s>', lambda _e: self._save())
        self.bind('<Escape>', lambda _e: self._close())
        self.protocol('WM_DELETE_WINDOW', self._close)

        self._refresh_provider_list()
        self._new_provider()

    def _field(self, parent, label, variable, placeholder):
        ctk.CTkLabel(parent, text=label, anchor='w').pack(fill='x', pady=(0, 4))
        ctk.CTkEntry(
            parent,
            textvariable=variable,
            placeholder_text=placeholder,
            height=34,
        ).pack(fill='x', pady=(0, 10))

    def _set_status(self, text: str, error: bool = False):
        self.status_label.configure(
            text=text,
            text_color=('#fca5a5' if error else '#cbd5e1'),
        )

    def _refresh_provider_list(self, select_key: str | None = None):
        for button in self.provider_buttons:
            button.destroy()
        self.provider_buttons.clear()

        providers = get_custom_providers()
        if not (vault.is_vault_unlocked() or vault.try_unlock_with_trusted_machine()):
            self._set_status('Secrets vault is locked. Unlock it from the tray before editing API keys.', error=True)
        if not providers:
            label = ctk.CTkLabel(
                self.provider_list,
                text='No custom providers yet.',
                text_color='#94a3b8',
            )
            label.pack(anchor='w', padx=6, pady=6)
            self.provider_buttons.append(label)
            return

        for provider in providers:
            is_selected = provider['id'] == (select_key or self.selected_provider_id)
            button = ctk.CTkButton(
                self.provider_list,
                text=f'{provider["label"]}\n{provider["id"]}',
                anchor='w',
                height=52,
                fg_color=('#1d4ed8' if is_selected else '#1f2937'),
                hover_color='#334155',
                command=lambda p=provider: self._select_provider(p),
            )
            button.pack(fill='x', padx=4, pady=4)
            self.provider_buttons.append(button)

    def _select_provider(self, provider: dict):
        self.selected_provider_id = provider['id']
        self.name_var.set(provider.get('label', ''))
        self.id_var.set(provider['id'])
        self.base_url_var.set(provider.get('base_url', ''))
        self.api_key_var.set(provider.get('api_key', ''))
        self._set_status(f'Editing "{provider["label"]}".')
        self._refresh_provider_list(select_key=provider['id'])

    def _new_provider(self):
        self.selected_provider_id = None
        self.name_var.set('')
        self.id_var.set('')
        self.base_url_var.set('')
        self.api_key_var.set('')
        self._set_status('Create a new API-key provider.')
        self._refresh_provider_list()

    def _save(self):
        if self.api_key_var.get().strip() and not (vault.is_vault_unlocked() or vault.try_unlock_with_trusted_machine()):
            self._set_status('Unlock the secrets vault from the tray before saving an API key.', error=True)
            return

        ok, message, provider_id = upsert_custom_provider({
            'label': self.name_var.get().strip(),
            'id': self.id_var.get().strip(),
            'base_url': self.base_url_var.get().strip(),
            'api_key': self.api_key_var.get().strip(),
            'auth_mode': 'apikey',
        })
        self._set_status(message, error=not ok)
        if not ok:
            return

        self.selected_provider_id = provider_id
        self._refresh_provider_list(select_key=provider_id)
        if self.on_changed:
            self.on_changed()

    def _delete(self):
        provider_id = self.id_var.get().strip()
        if not provider_id:
            self._set_status('Enter or select a provider first.', error=True)
            return

        if remove_custom_provider(provider_id):
            self._set_status(f'Deleted "{provider_id}".')
            self._new_provider()
            if self.on_changed:
                self.on_changed()
            return

        self._set_status(f'Could not delete "{provider_id}".', error=True)

    def _close(self):
        self.quit()
        self.destroy()


def show_provider_manager(on_changed: callable | None = None):
    try:
        app = ProviderManagerApp(on_changed=on_changed)
        app.mainloop()
    except Exception as e:
        print(f'[ProviderManager] Error: {e}')
