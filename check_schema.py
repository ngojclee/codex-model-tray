import config_manager as cfg


def mask(value: str | None) -> str:
	if not value:
		return '<empty>'
	if len(value) <= 8:
		return '<set>'
	return value[:4] + '...' + value[-4:]


def mask_auth(auth_data: dict | None) -> dict:
	if not isinstance(auth_data, dict):
		return {}
	masked = dict(auth_data)
	if 'OPENAI_API_KEY' in masked:
		masked['OPENAI_API_KEY'] = mask(str(masked['OPENAI_API_KEY']))
	tokens = masked.get('tokens')
	if isinstance(tokens, dict):
		masked['tokens'] = {key: mask(str(value)) for key, value in tokens.items()}
	return masked


p = cfg.get_current_provider()
print(f"provider: {p}")
print(f"effective key: {mask(cfg.get_effective_api_key(p))}")
print(f"custom key for {p}: {mask(cfg.get_custom_api_key(p))}")
print(f"custom krouter: {mask(cfg.get_custom_api_key('krouter'))}")
print(f"custom cliproxy: {mask(cfg.get_custom_api_key('cliproxy'))}")
print(f"auth.json: {mask_auth(cfg.read_auth())}")
