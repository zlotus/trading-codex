import importlib
import zlib
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

from trading_codex.baostock_download.constants import (
    BLACKLIST_ERROR_CODE,
    PROVIDER_CLIENT_VERSION,
)
from trading_codex.baostock_download.errors import (
    DailyAttemptLimitReached,
    ProviderBlacklisted,
    ProviderFailure,
)
from trading_codex.baostock_download.provider import BaoStockProvider
from trading_codex.baostock_download.raw_files import QueryRawFileStore
from trading_codex.baostock_download.requests import DownloadRequest
from trading_codex.baostock_download.runtime import (
    DailyAttemptCounter,
    GlobalDownloadLock,
)

ProgressCallback = Callable[[dict[str, object]], None]
SOCKET_RESPONSE_TIMEOUT_SECONDS = 60.0
SOCKET_RESPONSE_TERMINATOR = b"<![CDATA[]]>\n"


class SocketCounterGate:
    def __init__(
        self,
        *,
        counter: DailyAttemptCounter,
        socket_module: ModuleType,
        protocol_constants: ModuleType,
    ) -> None:
        self.counter = counter
        self.socket_module = socket_module
        self.protocol_constants = protocol_constants
        self._original_send = socket_module.send_msg
        self._phase = "unbound"
        self._request: DownloadRequest | None = None
        self._request_sends = 0
        self.network_attempts = 0

    @contextmanager
    def installed(self):
        self.socket_module.send_msg = self.send_msg
        try:
            yield self
        finally:
            self.socket_module.send_msg = self._original_send

    def bind_login(self) -> None:
        self._phase = "login"
        self._request = None
        self._request_sends = 0

    def bind_request(self, request: DownloadRequest) -> None:
        self._phase = "request"
        self._request = request
        self._request_sends = 0

    def bind_logout(self) -> None:
        self._phase = "logout"
        self._request = None
        self._request_sends = 0

    def send_msg(self, message: str) -> Any:
        if self._phase == "unbound":
            raise ProviderFailure("BaoStock socket send occurred outside download flow")
        kind = self._kind()
        request_id = self._request.request_id if self._request is not None else None
        self.counter.reserve(kind=kind, request_id=request_id)
        self.network_attempts += 1
        if self._request is not None:
            self._request_sends += 1
        response = self._send_message(message)
        code = self._response_code(response)
        if code == BLACKLIST_ERROR_CODE:
            self.counter.mark_blacklisted(
                {
                    "provider_code": code,
                    "phase": self._phase,
                    "kind": kind,
                    "request_id": request_id,
                }
            )
            raise ProviderBlacklisted(
                f"BaoStock returned blacklist error {BLACKLIST_ERROR_CODE}"
            )
        return response

    def _send_message(self, message: str) -> Any:
        context = getattr(self.socket_module, "context", None)
        provider_socket = getattr(context, "default_socket", None)
        if context is None:
            return self._original_send(message)
        if provider_socket is None:
            raise ProviderFailure("BaoStock provider socket is not connected")

        previous_timeout = provider_socket.gettimeout()
        try:
            provider_socket.settimeout(SOCKET_RESPONSE_TIMEOUT_SECONDS)
            provider_socket.sendall((message + "\n").encode("utf-8"))
            received = bytearray()
            while not received.endswith(SOCKET_RESPONSE_TERMINATOR):
                chunk = provider_socket.recv(8192)
                if not chunk:
                    raise ProviderFailure(
                        "BaoStock closed the socket before a complete response"
                    )
                received.extend(chunk)
            return self._decode_response(bytes(received))
        except ProviderFailure:
            raise
        except TimeoutError as exc:
            raise ProviderFailure("BaoStock socket response timed out") from exc
        except (OSError, UnicodeError, ValueError, zlib.error) as exc:
            raise ProviderFailure("BaoStock socket transport failed") from exc
        finally:
            try:
                provider_socket.settimeout(previous_timeout)
            except OSError:
                pass

    def _decode_response(self, response: bytes) -> str:
        header_length = getattr(self.protocol_constants, "MESSAGE_HEADER_LENGTH", None)
        separator = getattr(self.protocol_constants, "MESSAGE_SPLIT", None)
        compressed_types = getattr(
            self.protocol_constants,
            "COMPRESSED_MESSAGE_TYPE_TUPLE",
            (),
        )
        if not isinstance(header_length, int) or not isinstance(separator, str):
            raise ProviderFailure("BaoStock protocol constants are invalid")
        if len(response) < header_length:
            raise ProviderFailure("BaoStock response header is incomplete")
        header = response[:header_length].decode("utf-8")
        if compressed_types:
            header_parts = header.split(separator)
            if len(header_parts) < 3:
                raise ProviderFailure("BaoStock response header is malformed")
            if header_parts[1] in compressed_types:
                body_length = int(header_parts[2])
                compressed = response[header_length : header_length + body_length]
                return header + zlib.decompress(compressed).decode("utf-8")
        return response.decode("utf-8")

    def _kind(self) -> str:
        if self._phase != "request":
            return self._phase
        return "query" if self._request_sends == 0 else "page"

    def _response_code(self, response: Any) -> str | None:
        if not isinstance(response, str):
            return None
        header_length = getattr(self.protocol_constants, "MESSAGE_HEADER_LENGTH", None)
        separator = getattr(self.protocol_constants, "MESSAGE_SPLIT", None)
        if not isinstance(header_length, int) or not isinstance(separator, str):
            return None
        code, found, _ = response[header_length:].partition(separator)
        return code if found else None


def download(
    *,
    data_root: Path,
    state_root: Path,
    requests: tuple[DownloadRequest, ...],
    counter: DailyAttemptCounter | None = None,
    provider_module: Any | None = None,
    socket_module: ModuleType | None = None,
    protocol_constants: ModuleType | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    store = QueryRawFileStore(data_root)
    attempt_counter = counter or DailyAttemptCounter(state_root)
    downloaded: list[dict[str, object]] = []
    skipped = 0
    paused = False

    with GlobalDownloadLock(state_root):
        missing = [request for request in requests if not store.exists(request)]
        if not missing:
            return _result(
                status="complete",
                data_root=data_root,
                requests=requests,
                downloaded=downloaded,
                skipped=len(requests),
                remaining=0,
                network_attempts=0,
                counter=attempt_counter,
            )

        attempt_counter.assert_not_blacklisted()
        data_root.mkdir(parents=True, exist_ok=True)
        module = provider_module or importlib.import_module("baostock")
        if getattr(module, "__version__", None) != PROVIDER_CLIENT_VERSION:
            raise ProviderFailure(
                f"BaoStock client version must be {PROVIDER_CLIENT_VERSION}"
            )
        sock = socket_module or importlib.import_module("baostock.util.socketutil")
        constants = protocol_constants or importlib.import_module(
            "baostock.common.contants"
        )
        provider = BaoStockProvider(module)
        gate = SocketCounterGate(
            counter=attempt_counter,
            socket_module=sock,
            protocol_constants=constants,
        )
        try:
            with gate.installed():
                gate.bind_login()
                provider.login()
                for index, request in enumerate(requests, start=1):
                    if store.exists(request):
                        skipped += 1
                        continue
                    gate.bind_request(request)
                    batch = provider.fetch(request)
                    if batch.fields != request.expected_fields:
                        raise ProviderFailure(
                            f"BaoStock {request.operation} fields differ from the endpoint contract"
                        )
                    artifact = store.persist(request, batch)
                    event = {
                        "index": index,
                        "total": len(requests),
                        "operation": request.operation,
                        "request_id": request.request_id,
                        "rows": len(batch.rows),
                        "raw_file": str(artifact.path),
                        "content_sha256": artifact.content_sha256,
                    }
                    downloaded.append(event)
                    if progress is not None:
                        progress(event)
                gate.bind_logout()
                provider.logout()
        except DailyAttemptLimitReached:
            provider.close_local_socket()
            paused = True
        except ProviderBlacklisted as exc:
            attempt_counter.mark_blacklisted(
                {
                    "provider_code": BLACKLIST_ERROR_CODE,
                    "phase": "provider",
                    "reason": str(exc),
                }
            )
            provider.close_local_socket()
            raise
        except Exception:
            provider.close_local_socket()
            raise

        remaining = sum(not store.exists(request) for request in requests)
        return _result(
            status="paused_daily_limit" if paused else "passed",
            data_root=data_root,
            requests=requests,
            downloaded=downloaded,
            skipped=skipped,
            remaining=remaining,
            network_attempts=gate.network_attempts,
            counter=attempt_counter,
        )


def _result(
    *,
    status: str,
    data_root: Path,
    requests: tuple[DownloadRequest, ...],
    downloaded: list[dict[str, object]],
    skipped: int,
    remaining: int,
    network_attempts: int,
    counter: DailyAttemptCounter,
) -> dict[str, object]:
    return {
        "status": status,
        "network_access": network_attempts > 0,
        "data_root": str(data_root.resolve()),
        "raw_root": str((data_root / "raw").resolve()),
        "requests": len(requests),
        "downloaded": len(downloaded),
        "skipped_existing": skipped,
        "remaining": remaining,
        "network_attempts": network_attempts,
        "attempt_budget": counter.snapshot().as_dict(),
        "files": downloaded,
    }
