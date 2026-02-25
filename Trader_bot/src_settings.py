# src_settings.py
import json
import os
from typing import Any, Dict, Optional

OVERRIDES_PATH = "runtime_overrides.json"
PENDING_PATH = "pending_overrides.json"
ALLOWED_CHAT_PATH = "telegram_allowed_chat.json"

ALLOWED_KEYS = {
    "TRADE_PURSE_CAD": float,
    "USD_PER_CAD": float,
    "MAX_POSITIONS": int,
    "MIN_POSITION_USD": float,
}


def _read_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_json(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_overrides() -> Dict[str, Any]:
    return _read_json(OVERRIDES_PATH)


def save_overrides(overrides: Dict[str, Any]) -> None:
    _write_json(OVERRIDES_PATH, overrides)


def clear_overrides() -> None:
    if os.path.exists(OVERRIDES_PATH):
        os.remove(OVERRIDES_PATH)


def load_pending() -> Dict[str, Any]:
    return _read_json(PENDING_PATH)


def save_pending(pending: Dict[str, Any]) -> None:
    _write_json(PENDING_PATH, pending)


def clear_pending() -> None:
    if os.path.exists(PENDING_PATH):
        os.remove(PENDING_PATH)


def parse_value(key: str, raw_value: str):
    if key not in ALLOWED_KEYS:
        raise ValueError(f"Unknown setting: {key}")
    caster = ALLOWED_KEYS[key]
    try:
        return caster(raw_value)
    except Exception:
        raise ValueError(f"Invalid value for {key}: {raw_value}")


def propose_change(key: str, value: Any) -> Dict[str, Any]:
    """
    Store a pending change that requires CONFIRM.
    """
    if key not in ALLOWED_KEYS:
        raise ValueError(f"Unknown setting: {key}")

    current = load_overrides()
    pending = {
        "key": key,
        "value": value,
        "current_override": current.get(key, None),
    }
    save_pending(pending)
    return pending


def confirm_change() -> Dict[str, Any]:
    """
    Apply pending change into overrides.
    """
    pending = load_pending()
    if not pending or "key" not in pending:
        raise ValueError("No pending change to confirm.")

    key = pending["key"]
    value = pending["value"]

    overrides = load_overrides()
    overrides[key] = value
    save_overrides(overrides)
    clear_pending()
    return overrides


def cancel_change() -> None:
    clear_pending()


def apply_overrides_to_config(config_module) -> None:
    """
    Loads overrides and applies to the imported config module at runtime.
    """
    overrides = load_overrides()
    for k, v in overrides.items():
        if k in ALLOWED_KEYS:
            setattr(config_module, k, v)


def get_allowed_chat_id(config_module) -> Optional[int]:
    """
    Priority:
      1) config.TELEGRAM_ALLOWED_CHAT_ID if set
      2) telegram_allowed_chat.json (auto-registered on first /start)
    """
    cid = getattr(config_module, "TELEGRAM_ALLOWED_CHAT_ID", None)
    if isinstance(cid, int):
        return cid

    data = _read_json(ALLOWED_CHAT_PATH)
    stored = data.get("chat_id", None)
    return stored if isinstance(stored, int) else None


def register_allowed_chat_id(chat_id: int) -> None:
    _write_json(ALLOWED_CHAT_PATH, {"chat_id": chat_id})
