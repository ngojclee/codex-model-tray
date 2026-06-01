"""Shared window helpers for Codex Model Tray dialogs."""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path


def _resource_root() -> Path:
    bundle_root = getattr(sys, '_MEIPASS', None)
    if bundle_root:
        return Path(bundle_root)
    return Path(__file__).resolve().parents[1]


def set_app_window_icon(window: tk.Misc) -> None:
    """Apply the bundled app icon to a Tk/CustomTkinter window."""
    assets_dir = _resource_root() / 'assets'
    ico_path = assets_dir / 'icon.ico'
    png_path = assets_dir / 'ico.png'

    try:
        if ico_path.exists():
            window.iconbitmap(default=str(ico_path))
    except Exception:
        pass

    try:
        if png_path.exists():
            photo = tk.PhotoImage(file=str(png_path))
            window.iconphoto(True, photo)
            setattr(window, '_codex_model_tray_icon_photo', photo)
    except Exception:
        pass