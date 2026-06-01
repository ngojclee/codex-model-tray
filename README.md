# Codex Model Tray

Codex Model Tray is a Windows tray app for switching Codex Desktop / Codex CLI model and provider state without hand-editing multiple Codex files.

It also includes a tray toggle for `Start With Windows`, so you do not need to copy the app into the Startup folder manually.

It updates these Codex surfaces together:

- `config.toml`
- `auth.json`
- `state_5.sqlite`
- `sessions/**/*.jsonl`

It also keeps tray-side runtime files next to the app:

- `codex_model_tray.catalog.json`
- `codex_model_tray.secrets.json`
- `codex_model_tray.trust.json`

## What The App Stores

- `codex_model_tray.catalog.json`
  Provider/model metadata and pinned models. This file is safe to share after removing any private routing details you do not want to publish.
- `codex_model_tray.secrets.json`
  Password-encrypted vault for API keys and native auth backup. Do not commit or upload a real one.
- `codex_model_tray.trust.json`
  Trusted-machine cache for one device. Do not commit or upload a real one.

## Repo Files

The repo includes safe reference files:

- `codex_model_tray.catalog.example.json`
- `codex_model_tray.secrets.example.json`
- `codex_model_tray.trust.example.json`

These are examples only. They do not contain live keys, tokens, or trusted-machine data.

## Quick Start

1. Start Codex Desktop at least once so `%USERPROFILE%\\.codex` exists.
2. Run `CodexModelTray.exe`.
3. On first launch, create a vault password.
4. Add API keys through the tray UI if you use API providers.
5. Switch provider or run `Fix Threads (Safe)` if visible chats need repair.
6. Optional: enable `Start With Windows` from the tray menu.

## Development

Run from source:

```powershell
python main.py
```

Build the Windows exe:

```powershell
pyinstaller CodexModelTray.spec --clean
```

## Usage Guide

1. Download the latest release zip and extract it to a folder you want to keep.
2. Run `CodexModelTray.exe`.
3. Create a vault password on first launch.
4. Open the tray menu and choose the provider or model you want Codex to use.
5. Add provider API keys from the tray UI only when you use an API-key provider.
6. Use `Fix Threads (Safe)` to repair existing conversations after changing model or provider.
7. Use `Force Sync All Models` when you want every supported conversation record to match the currently selected main model.
8. Enable `Start With Windows` from the tray menu if you want the app to open automatically.

## Portable Notes

- The app resolves Codex home dynamically from `CODEX_HOME`, otherwise `%USERPROFILE%\\.codex`.
- `codex_model_tray.secrets.json` can move between machines, but the new machine still needs the vault password once.
- `codex_model_tray.trust.json` is machine-specific and should not be copied as a portable secret.

## Safety

This repo ignores:

- `codex_model_tray.secrets.json`
- `codex_model_tray.trust.json`
- `codex_model_tray.key`
- local Codex workspace state under `.omc/` and `.omx/`

That keeps normal git pushes from accidentally including live secrets or local runtime state.
