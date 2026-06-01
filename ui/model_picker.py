"""
Model picker popup using CustomTkinter.
Dark mode, searchable, add/remove any model.
Optimized: lightweight rows, batch render, canvas scroll.
"""

import tkinter as tk
import customtkinter as ctk
import config_manager as cfg
from providers import (
    get_models_for_provider,
    add_model, remove_model,
)
from ui.window_utils import set_app_window_icon

# ── Lightweight row: pin + select + delete ──────────────────
# Using tk.Frame + tk.Label/Button for speed (CTk widgets are ~5x heavier)

_BG = '#1a1a2e'
_ROW_BG = '#16213e'
_ROW_HOVER = '#1f3460'
_ROW_ACTIVE = '#1b3a20'
_TEXT = '#e0e0e0'
_TEXT_DIM = '#777'
_GREEN = '#90EE90'
_GOLD = '#FFD700'
_RED = '#F44336'


class ModelPickerApp(ctk.CTk):

    def __init__(self, provider_key: str, current_model: str, on_select: callable):
        super().__init__()
        self.on_select = on_select
        self.provider_key = provider_key
        self.current_model = current_model
        self.all_models = get_models_for_provider(provider_key)
        self.result = None

        self.title('Codex Model Picker')
        set_app_window_icon(self)
        self.geometry('500x600')
        self.configure(fg_color=_BG)
        self.attributes('-topmost', True)
        self.resizable(False, True)

        # Center
        self.update_idletasks()
        x = (self.winfo_screenwidth() - 500) // 2
        y = (self.winfo_screenheight() - 600) // 2
        self.geometry(f'500x600+{x}+{y}')

        ctk.set_appearance_mode('dark')

        # ── Header ──
        hdr = ctk.CTkFrame(self, fg_color='transparent', height=40)
        hdr.pack(fill='x', padx=12, pady=(12, 4))
        hdr.pack_propagate(False)

        ctk.CTkLabel(
            hdr, text=f'🚀 {current_model}',
            font=ctk.CTkFont(size=15, weight='bold'),
        ).pack(side='left')

        ctk.CTkButton(
            hdr, text='➕ Add', width=70, height=28,
            font=ctk.CTkFont(size=11),
            fg_color='#2E7D32', hover_color='#388E3C',
            command=self._on_add_model,
        ).pack(side='right')

        # ── Search ──
        self.search_var = ctk.StringVar()
        self.search_var.trace_add('write', self._on_search)
        ctk.CTkEntry(
            self, placeholder_text='🔍 Search...',
            textvariable=self.search_var,
            height=32, font=ctk.CTkFont(size=13),
        ).pack(fill='x', padx=12, pady=(0, 6))

        # ── Scrollable list (native tkinter canvas for speed) ──
        container = tk.Frame(self, bg=_BG)
        container.pack(fill='both', expand=True, padx=4, pady=(0, 4))

        self.canvas = tk.Canvas(container, bg=_BG, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(container, orient='vertical', command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=_BG)

        self.inner.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.create_window((0, 0), window=self.inner, anchor='nw', tags='inner')
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

        # Mouse wheel scrolling
        self.canvas.bind_all('<MouseWheel>', lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), 'units'))

        # Resize inner width to canvas
        self.canvas.bind('<Configure>', self._resize_inner)

        self.pinned_models = cfg.get_pinned_models()
        self.row_widgets = []  # [(model_id, desc, row_frame)]
        self._batch_idx = 0
        self._batch_create()

        self.bind('<Escape>', lambda e: self._close())
        self.protocol('WM_DELETE_WINDOW', self._close)

    def _resize_inner(self, event):
        self.canvas.itemconfigure('inner', width=event.width)

    # ── Batch rendering (20 at a time to avoid freeze) ──

    def _batch_create(self):
        BATCH = 25
        end = min(self._batch_idx + BATCH, len(self.all_models))
        for i in range(self._batch_idx, end):
            self._create_row(self.all_models[i])
        self._batch_idx = end
        if self._batch_idx < len(self.all_models):
            self.after_idle(self._batch_create)

    def _create_row(self, model_tuple):
        model_id, desc, icon = model_tuple
        is_active = model_id == self.current_model
        is_pinned = model_id in self.pinned_models

        bg = _ROW_ACTIVE if is_active else _ROW_BG
        fg = _GREEN if is_active else _TEXT

        row = tk.Frame(self.inner, bg=bg, pady=3, padx=4)
        row.pack(fill='x', padx=4, pady=1)

        # Pin ★/☆
        pin_lbl = tk.Label(
            row, text='★' if is_pinned else '☆',
            fg=_GOLD if is_pinned else _TEXT_DIM,
            bg=bg, font=('Segoe UI', 13), cursor='hand2', width=2,
        )
        pin_lbl.pack(side='left')

        def _toggle_pin(mid=model_id, lbl=pin_lbl, r=row):
            self.pinned_models = cfg.toggle_pinned_model(mid)
            p = mid in self.pinned_models
            lbl.configure(text='★' if p else '☆', fg=_GOLD if p else _TEXT_DIM)

        pin_lbl.bind('<Button-1>', lambda e: _toggle_pin())

        # Model label (clickable)
        check = ' ✅' if is_active else ''
        label_text = f'{icon}  {model_id}{check}'

        model_lbl = tk.Label(
            row, text=label_text, fg=fg, bg=bg,
            font=('Segoe UI', 11), anchor='w', cursor='hand2',
        )
        model_lbl.pack(side='left', fill='x', expand=True, padx=(4, 0))

        def _select(mid=model_id):
            self.result = mid
            self._close()
            if self.on_select:
                self.on_select(mid)

        model_lbl.bind('<Button-1>', lambda e: _select())

        # Hover effect
        def _enter(r=row, m=model_lbl, p=pin_lbl):
            c = _ROW_HOVER if not is_active else _ROW_ACTIVE
            for w in (r, m, p):
                w.configure(bg=c)

        def _leave(r=row, m=model_lbl, p=pin_lbl):
            c = _ROW_ACTIVE if is_active else _ROW_BG
            for w in (r, m, p):
                w.configure(bg=c)

        for w in (row, model_lbl, pin_lbl):
            w.bind('<Enter>', lambda e: _enter())
            w.bind('<Leave>', lambda e: _leave())

        # Delete 🗑
        del_lbl = tk.Label(
            row, text='✕', fg=_TEXT_DIM, bg=bg,
            font=('Segoe UI', 10), cursor='hand2', width=2,
        )
        del_lbl.pack(side='right')
        del_lbl.bind('<Enter>', lambda e: del_lbl.configure(fg=_RED))
        del_lbl.bind('<Leave>', lambda e: del_lbl.configure(fg=_TEXT_DIM))

        def _delete(mid=model_id, r=row):
            remove_model(mid)
            r.pack_forget()
            r.destroy()
            self.row_widgets = [(m, d, w) for m, d, w in self.row_widgets if m != mid]

        del_lbl.bind('<Button-1>', lambda e: _delete())

        self.row_widgets.append((model_id, desc, row))

    # ── Search ──

    def _on_search(self, *_):
        q = self.search_var.get().lower().strip()
        for mid, desc, row in self.row_widgets:
            row.pack_forget()
        for mid, desc, row in self.row_widgets:
            if not q or q in mid.lower() or q in desc.lower():
                row.pack(fill='x', padx=4, pady=1)

    # ── Add dialog ──

    def _on_add_model(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title('➕ Add Model')
        set_app_window_icon(dialog)
        dialog.geometry('400x220')
        dialog.attributes('-topmost', True)
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        x = self.winfo_x() + (self.winfo_width() - 400) // 2
        y = self.winfo_y() + (self.winfo_height() - 220) // 2
        dialog.geometry(f'400x220+{x}+{y}')

        ctk.CTkLabel(dialog, text='Add Model', font=ctk.CTkFont(size=15, weight='bold')).pack(pady=(14, 6))

        ctk.CTkLabel(dialog, text='Model ID:', anchor='w').pack(fill='x', padx=16)
        id_var = ctk.StringVar()
        id_entry = ctk.CTkEntry(dialog, textvariable=id_var, height=30,
                                 placeholder_text='cx/my-model or gpt-6')
        id_entry.pack(fill='x', padx=16, pady=(0, 6))
        id_entry.focus_set()

        ctk.CTkLabel(dialog, text='Description:', anchor='w').pack(fill='x', padx=16)
        desc_var = ctk.StringVar()
        ctk.CTkEntry(dialog, textvariable=desc_var, height=30,
                      placeholder_text='Optional label').pack(fill='x', padx=16, pady=(0, 10))

        def _save():
            mid = id_var.get().strip()
            if not mid:
                return
            ok = add_model(mid, desc_var.get().strip())
            dialog.destroy()
            if ok:
                self._refresh_list()

        bf = ctk.CTkFrame(dialog, fg_color='transparent')
        bf.pack(fill='x', padx=16)
        ctk.CTkButton(bf, text='💾 Add', command=_save,
                       fg_color='#2E7D32', hover_color='#388E3C',
                       height=32, width=100).pack(side='left', padx=(0, 8))
        ctk.CTkButton(bf, text='Cancel', command=dialog.destroy,
                       fg_color='#555', hover_color='#666',
                       height=32, width=80).pack(side='left')

        dialog.bind('<Return>', lambda e: _save())
        dialog.bind('<Escape>', lambda e: dialog.destroy())

    def _refresh_list(self):
        for _, _, row in self.row_widgets:
            row.destroy()
        self.row_widgets.clear()
        self.all_models = get_models_for_provider(self.provider_key)
        self._batch_idx = 0
        self._batch_create()

    # ── Lifecycle ──

    def _close(self):
        self.canvas.unbind_all('<MouseWheel>')
        self.quit()
        self.destroy()


def show_model_picker(provider_key: str, current_model: str, on_select: callable):
    try:
        app = ModelPickerApp(provider_key, current_model, on_select)
        app.mainloop()
        return app.result
    except Exception as e:
        print(f'[ModelPicker] Error: {e}')
        return None
