import os
import tempfile
from pathlib import Path

from trading_codex.baostock_download.envelope import (
    encode_envelope,
    verify_envelope,
    verify_envelope_bytes,
)
from trading_codex.baostock_download.requests import (
    DownloadRequest,
)
from trading_codex.data.models import ProviderBatch, RawArtifact


class QueryRawFileStore:
    """Raw files addressed only by their exact provider request."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.raw_root = self.data_root / "raw"

    def path_for(self, request: DownloadRequest) -> Path:
        return request.raw_path(self.data_root)

    def exists(self, request: DownloadRequest) -> bool:
        # Deliberately do not open or validate existing files. That belongs to
        # the offline data-processing tool.
        return self.path_for(request).is_file()

    def persist(self, request: DownloadRequest, batch: ProviderBatch) -> RawArtifact:
        if batch.source != "baostock" or batch.operation != request.operation:
            raise ValueError("provider batch does not match the download request")
        if batch.query != request.raw_query:
            raise ValueError("provider batch query does not match the download request")
        if batch.fields != request.expected_fields:
            raise ValueError("provider fields differ from the endpoint contract")

        payload, content_sha256 = encode_envelope(batch)
        path = self.path_for(request)
        encoded = verify_envelope_bytes(
            payload,
            path=path,
            raw_root=self.raw_root,
        )
        if encoded.artifact.content_sha256 != content_sha256 or encoded.batch != batch:
            raise ValueError("encoded envelope differs from the downloaded payload")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if not path.exists():
                os.replace(temporary, path)
                _fsync_directory(path.parent)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        # Re-read the durable file. Later processing repeats this verification
        # independently instead of trusting the downloader result.
        verified = verify_envelope(path, raw_root=self.raw_root)
        if (
            verified.artifact.content_sha256 != content_sha256
            or verified.batch != batch
            or verified.artifact.path != self.path_for(request)
        ):
            raise ValueError("persisted envelope differs from the downloaded payload")
        return verified.artifact


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
