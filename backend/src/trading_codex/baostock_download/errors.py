class BaoStockDownloadError(RuntimeError):
    """Base error for the BaoStock raw downloader."""


class RequestInputError(BaoStockDownloadError):
    """A JSONL request does not match a supported BaoStock endpoint."""


class ManifestError(BaoStockDownloadError):
    """A manifest or planner input violates the frozen contract."""


class StoragePreflightError(BaoStockDownloadError):
    """The target storage cannot safely accept a fetch."""


class StateError(BaoStockDownloadError):
    """The global provider state is unavailable or inconsistent."""


class ProviderLockError(StateError):
    """Another process owns the global BaoStock provider lock."""


class BudgetExceeded(StateError):
    """A socket attempt would exceed a persistent request budget."""


class DailyAttemptLimitReached(BudgetExceeded):
    """The simple downloader reached its calendar-day stop boundary."""


class ProviderFailure(BaoStockDownloadError):
    """BaoStock returned an error or malformed response."""


class ProviderBlacklisted(ProviderFailure):
    """BaoStock returned the persistent blacklist error."""


class SchemaDriftError(BaoStockDownloadError):
    """Provider fields differ from the frozen endpoint contract."""


class OfflineSyncError(BaoStockDownloadError):
    """Immutable raw data cannot be safely published offline."""
