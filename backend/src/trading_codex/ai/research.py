from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from trading_codex.domain.hashing import canonical_sha256

RESEARCH_MANIFEST_VERSION = "isolated-ai-research-v1"


class ResearchIsolationError(RuntimeError):
    """Research data does not preserve the declared physical split boundary."""


class ResearchSplit(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


@dataclass(frozen=True)
class ResearchPartition:
    split: ResearchSplit
    root: Path
    start_date: date
    end_date: date
    content_sha256: str

    def __post_init__(self) -> None:
        root = self.root.expanduser()
        if root.is_symlink():
            raise ResearchIsolationError(f"{self.split.value} root cannot be a symlink")
        root = root.resolve()
        object.__setattr__(self, "root", root)
        if not root.is_dir():
            raise ResearchIsolationError(f"{self.split.value} root must be a local directory")
        if self.end_date < self.start_date:
            raise ResearchIsolationError(f"{self.split.value} date range is reversed")
        if len(self.content_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.content_sha256
        ):
            raise ResearchIsolationError(
                f"{self.split.value} content hash must be a lowercase SHA-256 digest"
            )

    def verify(self) -> None:
        actual = directory_sha256(self.root)
        if actual != self.content_sha256:
            raise ResearchIsolationError(
                f"{self.split.value} content hash mismatch: expected "
                f"{self.content_sha256}, got {actual}"
            )


@dataclass(frozen=True)
class IsolatedResearchDataset:
    train: ResearchPartition
    validation: ResearchPartition
    test: ResearchPartition
    version: str = RESEARCH_MANIFEST_VERSION

    def __post_init__(self) -> None:
        partitions = (self.train, self.validation, self.test)
        if tuple(item.split for item in partitions) != tuple(ResearchSplit):
            raise ResearchIsolationError("research partitions must be train, validation, and test")
        if not self.train.end_date < self.validation.start_date:
            raise ResearchIsolationError("training and validation periods must not overlap")
        if not self.validation.end_date < self.test.start_date:
            raise ResearchIsolationError("validation and test periods must not overlap")
        for index, left in enumerate(partitions):
            for right in partitions[index + 1 :]:
                if _paths_overlap(left.root, right.root):
                    raise ResearchIsolationError(
                        f"{left.split.value} and {right.split.value} roots must be isolated"
                    )
        if len({item.content_sha256 for item in partitions}) != len(partitions):
            raise ResearchIsolationError("research partitions cannot share the same artifact")
        if self.version != RESEARCH_MANIFEST_VERSION:
            raise ResearchIsolationError("unsupported research manifest version")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(
            {
                "version": self.version,
                "splits": {
                    item.split.value: {
                        "root": str(item.root),
                        "start_date": item.start_date,
                        "end_date": item.end_date,
                        "content_sha256": item.content_sha256,
                    }
                    for item in (self.train, self.validation, self.test)
                },
            }
        )

    def verify(self) -> None:
        self.train.verify()
        self.validation.verify()
        self.test.verify()


@dataclass(frozen=True)
class DevelopmentDataset:
    train: ResearchPartition
    validation: ResearchPartition
    manifest_fingerprint: str


@dataclass(frozen=True)
class SealedTestDataset:
    test: ResearchPartition
    manifest_fingerprint: str


@dataclass(frozen=True)
class FrozenResearchCandidate:
    payload_json: str
    candidate_sha256: str
    frozen_at: datetime


@dataclass(frozen=True)
class OfflineResearchRun:
    manifest_fingerprint: str
    candidate_sha256: str
    evaluation: Mapping[str, object]
    completed_at: datetime


class ResearchDeveloper(Protocol):
    def develop(self, dataset: DevelopmentDataset) -> Mapping[str, object]: ...


class ResearchEvaluator(Protocol):
    def evaluate(
        self,
        candidate: FrozenResearchCandidate,
        dataset: SealedTestDataset,
    ) -> Mapping[str, object]: ...


class OfflineResearchRunner:
    """Reveals the test root only after the candidate payload has been frozen."""

    def __init__(
        self,
        dataset: IsolatedResearchDataset,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        dataset.verify()
        self.dataset = dataset
        self._now = now or (lambda: datetime.now(UTC))

    def freeze(self, developer: ResearchDeveloper) -> FrozenResearchCandidate:
        development = DevelopmentDataset(
            train=self.dataset.train,
            validation=self.dataset.validation,
            manifest_fingerprint=self.dataset.fingerprint,
        )
        payload = developer.develop(development)
        payload_json = _mapping_json(payload, field="research candidate")
        frozen_at = _aware_utc(self._now(), field="candidate frozen_at")
        return FrozenResearchCandidate(
            payload_json=payload_json,
            candidate_sha256=canonical_sha256(
                {
                    "manifest_fingerprint": self.dataset.fingerprint,
                    "payload": json.loads(payload_json),
                }
            ),
            frozen_at=frozen_at,
        )

    def evaluate(
        self,
        candidate: FrozenResearchCandidate,
        evaluator: ResearchEvaluator,
    ) -> OfflineResearchRun:
        expected = canonical_sha256(
            {
                "manifest_fingerprint": self.dataset.fingerprint,
                "payload": json.loads(candidate.payload_json),
            }
        )
        if candidate.candidate_sha256 != expected:
            raise ResearchIsolationError("research candidate changed after it was frozen")
        sealed_test = SealedTestDataset(
            test=self.dataset.test,
            manifest_fingerprint=self.dataset.fingerprint,
        )
        evaluation = evaluator.evaluate(candidate, sealed_test)
        _mapping_json(evaluation, field="research evaluation")
        return OfflineResearchRun(
            manifest_fingerprint=self.dataset.fingerprint,
            candidate_sha256=candidate.candidate_sha256,
            evaluation=evaluation,
            completed_at=_aware_utc(self._now(), field="research completed_at"),
        )


def load_research_manifest(path: str | Path) -> IsolatedResearchDataset:
    manifest_path = Path(path).resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"version", "splits"}:
        raise ResearchIsolationError("research manifest fields are invalid")
    splits = payload["splits"]
    if not isinstance(splits, dict) or set(splits) != {item.value for item in ResearchSplit}:
        raise ResearchIsolationError("research manifest must contain exactly three splits")

    def partition(split: ResearchSplit) -> ResearchPartition:
        item = splits[split.value]
        expected = {"root", "start_date", "end_date", "content_sha256"}
        if not isinstance(item, dict) or set(item) != expected:
            raise ResearchIsolationError(f"{split.value} manifest fields are invalid")
        root = Path(item["root"])
        if not root.is_absolute():
            root = manifest_path.parent / root
        return ResearchPartition(
            split=split,
            root=root,
            start_date=date.fromisoformat(item["start_date"]),
            end_date=date.fromisoformat(item["end_date"]),
            content_sha256=item["content_sha256"],
        )

    return IsolatedResearchDataset(
        train=partition(ResearchSplit.TRAIN),
        validation=partition(ResearchSplit.VALIDATION),
        test=partition(ResearchSplit.TEST),
        version=payload["version"],
    )


def directory_sha256(root: str | Path) -> str:
    base = Path(root).resolve()
    if not base.is_dir():
        raise ResearchIsolationError("research artifact root must be a directory")
    digest = hashlib.sha256()
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise ResearchIsolationError("research artifacts cannot contain symlinks")
        if not path.is_file():
            continue
        relative = path.relative_to(base).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _mapping_json(value: Mapping[str, object], *, field: str) -> str:
    if not isinstance(value, Mapping):
        raise ResearchIsolationError(f"{field} must be a mapping")
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ResearchIsolationError(f"{field} must be canonical JSON") from error


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ResearchIsolationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
