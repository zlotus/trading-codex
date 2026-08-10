import os
from pathlib import Path

PROVIDER = "baostock"
PROVIDER_CLIENT_VERSION = "00.9.30"
BLACKLIST_ERROR_CODE = "10001011"
BLACKLIST_RULES_CAPTURED_AT = "2026-08-09T19:26:55+08:00"
BLACKLIST_PAGE_SHA256 = "fe0bad2d5c6e6cb6ff415e291510c171ba758b7deb0001ed60a8a108c7933a18"
BLACKLIST_RULES_SHA256 = "0ce1b6d6e3f386fc7080acf6790d3ee2dfb35ca7dd79beb286da5a39e229d3a8"

PROVIDER_CALENDAR_DAY_HARD_LIMIT = 50_000
PROJECT_CALENDAR_DAY_HARD_LIMIT = 45_000
DEFAULT_CALENDAR_DAY_LIMIT = 2_000
DEFAULT_ROLLING_24H_LIMIT = 2_000
DEFAULT_SESSION_ATTEMPT_LIMIT = 100
MINIMUM_INTERVAL_SECONDS = 3.0
PILOT_INTERVAL_SECONDS = 30.0
DEFAULT_MAX_ITEMS = 1
MAX_SESSION_ITEMS = 100

WARN_FREE_BYTES = 150 * 1024**3
FAIL_FREE_BYTES = 100 * 1024**3
PEAK_RESERVE_BYTES = 20 * 1024**3
MAX_USED_PERCENT = 90.0

DEFAULT_DATA_ROOT = Path("/mnt/exos_1t/quant/baostock")


def provider_rule_snapshot() -> dict[str, str]:
    return {
        "captured_at": BLACKLIST_RULES_CAPTURED_AT,
        "page_sha256": BLACKLIST_PAGE_SHA256,
        "rules_asset_sha256": BLACKLIST_RULES_SHA256,
        "daily_limit_text": "每日API请求不能超过5万次，并且不能并发连接访问，超过后进入黑名单控制",
        "blacklist_error_code": BLACKLIST_ERROR_CODE,
    }


def default_state_root() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured) if configured else Path.home() / ".local" / "state"
    return base / "trading-codex" / "baostock"
