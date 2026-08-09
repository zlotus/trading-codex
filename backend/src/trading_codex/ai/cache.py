from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from trading_codex.ai.contracts import ProviderCompletion


class CompletionCache(Protocol):
    def get(self, cache_key: str) -> ProviderCompletion | None: ...

    def put(self, cache_key: str, completion: ProviderCompletion) -> None: ...


@dataclass
class MemoryCompletionCache:
    _items: dict[str, ProviderCompletion]

    def __init__(self) -> None:
        self._items = {}

    def get(self, cache_key: str) -> ProviderCompletion | None:
        return self._items.get(cache_key)

    def put(self, cache_key: str, completion: ProviderCompletion) -> None:
        existing = self._items.get(cache_key)
        if existing is not None and existing != completion:
            raise ValueError("AI cache key already contains different content")
        self._items[cache_key] = completion


class FileCompletionCache:
    """Content-addressed, immutable cache for structured provider responses."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, cache_key: str) -> ProviderCompletion | None:
        path = self._path(cache_key)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ValueError("AI cache entry must be a regular file")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"cache_key", "completion"}
            or payload.get("cache_key") != cache_key
        ):
            raise ValueError("AI cache entry key is inconsistent")
        completion = payload.get("completion")
        expected = {
            "content",
            "model",
            "input_tokens",
            "output_tokens",
            "provider_request_id",
        }
        if not isinstance(completion, dict) or set(completion) != expected:
            raise ValueError("AI cache entry is malformed")
        try:
            return ProviderCompletion(**completion)
        except (TypeError, ValueError) as error:
            raise ValueError("AI cache entry is malformed") from error

    def put(self, cache_key: str, completion: ProviderCompletion) -> None:
        path = self._path(cache_key)
        payload = {
            "cache_key": cache_key,
            "completion": asdict(completion),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_text(encoding="utf-8") != encoded:
                raise ValueError("AI cache key already contains different content")
            return
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{cache_key}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_name, path)
            except FileExistsError:
                if path.read_text(encoding="utf-8") != encoded:
                    raise ValueError("AI cache key already contains different content")
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _path(self, cache_key: str) -> Path:
        if len(cache_key) != 64 or any(
            character not in "0123456789abcdef" for character in cache_key
        ):
            raise ValueError("AI cache key must be a lowercase SHA-256 digest")
        return self.root / cache_key[:2] / f"{cache_key}.json"
