"""Small, private local configuration; credentials never enter settings output."""

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from .errors import ContractError


# Verified against https://api-docs.deepseek.com/quick_start/pricing (2026-10-15).
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_SETTINGS = {
    "version": 1,
    "deepseek": {
        "base_url": DEFAULT_DEEPSEEK_BASE_URL,
        "model": DEFAULT_DEEPSEEK_MODEL,
        "timeout": 90,
        "max_tokens": 6000,
        "max_calls": 3,
        "allow_custom_base_url": False,
    },
    "local": {},
}
_SECRET_FIELDS = {"api_key", "apikey", "authorization", "access_token", "password", "secret", "credentials"}


def _no_credentials(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower().replace("-", "_") in _SECRET_FIELDS:
                raise ContractError("Credentials must be stored through config deepseek, never in settings")
            _no_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _no_credentials(child)
    elif isinstance(value, str) and re.search(r"\bsk-[A-Za-z0-9_-]{16,}", value):
        raise ContractError("A credential-like value cannot be stored in settings")


def validate_base_url(base_url, allow_custom=False):
    """Require an explicit opt-in before sending a credential to another host."""
    if not isinstance(base_url, str):
        raise ContractError("DeepSeek base_url must be an HTTPS URL")
    try:
        url = urlsplit(base_url)
        port = url.port
    except ValueError:
        raise ContractError("Invalid DeepSeek base_url") from None
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or any(c.isspace() for c in base_url)):
        raise ContractError("DeepSeek base_url must be HTTPS without credentials, query, or fragment")
    if (url.hostname != "api.deepseek.com" or port not in (None, 443)) and allow_custom is not True:
        raise ContractError("Custom provider requires explicit deepseek.allow_custom_base_url=true")
    return base_url.rstrip("/")


def _validate_settings(settings):
    _no_credentials(settings)
    try:
        json.dumps(settings, allow_nan=False)
    except (TypeError, ValueError):
        raise ContractError("Configuration values must be finite JSON values") from None
    if set(settings) - {"version", "deepseek", "local"} or settings.get("version") != 1:
        raise ContractError("Unsupported configuration format")
    cloud = settings.get("deepseek")
    if not isinstance(cloud, dict) or not isinstance(settings.get("local"), dict):
        raise ContractError("Configuration sections must be JSON objects")
    if set(cloud) - set(DEFAULT_SETTINGS["deepseek"]):
        raise ContractError("Unknown DeepSeek setting")
    if not isinstance(cloud.get("allow_custom_base_url"), bool):
        raise ContractError("allow_custom_base_url must be a boolean")
    validate_base_url(cloud.get("base_url"), cloud["allow_custom_base_url"])
    if not isinstance(cloud.get("model"), str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}", cloud["model"]):
        raise ContractError("Invalid DeepSeek model ID")
    for name, maximum in (("timeout", 300), ("max_tokens", 32000), ("max_calls", 10)):
        value = cloud.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum:
            raise ContractError(f"DeepSeek {name} must be positive and at most {maximum}")
        if name != "timeout" and not isinstance(value, int):
            raise ContractError(f"DeepSeek {name} must be an integer")


class ConfigStore:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get("SPLICE_CONFIG_DIR") or Path.home() / ".config" / "splice").expanduser()
        self.settings_path = self.directory / "settings.json"
        self.credentials_path = self.directory / "credentials.json"

    def _prepare(self):
        if self.directory.is_symlink():
            raise ContractError("Configuration directory must not be a symlink")
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
        except OSError:
            raise ContractError("Cannot create private configuration directory") from None

    def _read(self, path, default):
        if self.directory.exists():
            self._prepare()
        if path.is_symlink():
            raise ContractError("Configuration files must not be symlinks")
        if not path.exists():
            return deepcopy(default)
        try:
            path.chmod(0o600)
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ContractError("Cannot read private configuration JSON") from None
        if not isinstance(result, dict):
            raise ContractError("Configuration file must contain a JSON object")
        return result

    def _write(self, path, value):
        self._prepare()
        if path.is_symlink():
            raise ContractError("Configuration files must not be symlinks")
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".splice-", dir=self.directory)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except (OSError, ValueError, TypeError):
            raise ContractError("Cannot write private configuration JSON") from None
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def init(self):
        settings = self.load()
        self._write(self.settings_path, settings)
        if not self.credentials_path.exists():
            self._write(self.credentials_path, {})
        return self.show()

    def load(self):
        saved = self._read(self.settings_path, {})
        settings = deepcopy(DEFAULT_SETTINGS)
        for section, value in saved.items():
            if section in {"deepseek", "local"} and isinstance(value, dict):
                settings[section].update(value)
            else:
                settings[section] = value
        _validate_settings(settings)
        return settings

    def show(self):
        result = self.load()
        result["credentials"] = {"deepseek": "configured" if self.get_api_key() else "missing"}
        return result

    def set_api_key(self, key):
        if not isinstance(key, str) or not key.strip() or len(key) > 4096:
            raise ContractError("An API key is required")
        key = key.strip()
        if not key.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in key):
            raise ContractError("Invalid API key format")
        self.init()
        self._write(self.credentials_path, {"deepseek_api_key": key})

    def get_api_key(self):
        """An explicit environment credential takes precedence over the file."""
        environment_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if environment_key:
            return environment_key
        value = self._read(self.credentials_path, {}).get("deepseek_api_key", "")
        if not isinstance(value, str):
            raise ContractError("Invalid credentials format")
        return value.strip()

    def clear_api_key(self):
        self._write(self.credentials_path, {})

    def update(self, section, values):
        if section not in {"deepseek", "local"} or not isinstance(values, dict):
            raise ContractError("Configuration update requires a valid section and JSON object")
        settings = self.load()
        settings[section].update(deepcopy(values))
        _validate_settings(settings)
        self._write(self.settings_path, settings)
        return self.show()
