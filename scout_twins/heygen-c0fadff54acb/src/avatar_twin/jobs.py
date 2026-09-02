from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import hashlib
import hmac
import json

from .models import ValidationError, VideoProject, new_id, utc_now
from .renderer import RenderEngine


JOB_STATES = {"queued", "rendering", "completed", "failed"}


@dataclass(slots=True)
class JobRecord:
    id: str
    project_id: str
    state: str = "queued"
    progress: float = 0.0
    output_dir: str = ""
    artifact: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    callback_url: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "JobRecord":
        return cls(
            id=str(value["id"]), project_id=str(value["project_id"]),
            state=str(value.get("state") or "queued"), progress=float(value.get("progress", 0)),
            output_dir=str(value.get("output_dir") or ""), artifact=dict(value.get("artifact") or {}),
            error=str(value.get("error") or ""), created_at=str(value.get("created_at") or utc_now()),
            updated_at=str(value.get("updated_at") or utc_now()), callback_url=str(value.get("callback_url") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.records = self.root / "jobs"
        self.outputs = self.root / "outputs"
        self.records.mkdir(parents=True, exist_ok=True)
        self.outputs.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _path(self, job_id: str) -> Path:
        if not job_id.startswith("job_") or not job_id[4:].isalnum():
            raise ValidationError("invalid job id")
        return self.records / f"{job_id}.json"

    def create(self, project: VideoProject) -> JobRecord:
        project.validate(require_approved=True)
        job_id = new_id("job")
        record = JobRecord(
            id=job_id, project_id=project.id, output_dir=str(self.outputs / job_id),
            callback_url=project.callback_url,
        )
        self.save(record)
        return record

    def save(self, record: JobRecord) -> None:
        if record.state not in JOB_STATES or not 0 <= record.progress <= 1:
            raise ValidationError("invalid job state")
        record.updated_at = utc_now()
        payload = json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n"
        path = self._path(record.id)
        temporary = path.with_suffix(".tmp")
        with self._lock:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(path)

    def get(self, job_id: str) -> JobRecord:
        path = self._path(job_id)
        with self._lock:
            if not path.is_file():
                raise KeyError(job_id)
            return JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[JobRecord]:
        records = []
        with self._lock:
            for path in sorted(self.records.glob("job_*.json"), reverse=True):
                records.append(JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        return records


@dataclass(slots=True)
class WebhookClient:
    secret: bytes
    allowed_hosts: frozenset[str] = frozenset()
    timeout_s: float = 10.0

    def deliver(self, callback_url: str, event: dict[str, Any]) -> None:
        if not callback_url:
            return
        parsed = urlparse(callback_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or host not in self.allowed_hosts:
            raise ValidationError("callback URL is not an explicitly allowed HTTPS host")
        payload = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self.secret, payload, hashlib.sha256).hexdigest()
        request = Request(callback_url, data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "X-Avatar-Twin-Event": str(event.get("type") or ""),
            "X-Avatar-Twin-Signature-256": f"sha256={signature}",
        })
        with urlopen(request, timeout=self.timeout_s) as response:
            if response.status // 100 != 2:
                raise RuntimeError(f"callback returned HTTP {response.status}")


class RenderQueue:
    def __init__(self, store: JobStore, renderer: RenderEngine, *, workers: int = 2,
                 webhook_client: WebhookClient | None = None):
        self.store = store
        self.renderer = renderer
        self.webhook_client = webhook_client
        self.pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="avatar-render")
        self._futures: dict[str, Future[None]] = {}

    def submit(self, project: VideoProject) -> JobRecord:
        record = self.store.create(project)
        self._futures[record.id] = self.pool.submit(self._run, record.id, project.clone())
        return record

    def _run(self, job_id: str, project: VideoProject) -> None:
        record = self.store.get(job_id)
        record.state = "rendering"
        record.progress = 0.1
        self.store.save(record)
        try:
            artifact = self.renderer.render(project, record.output_dir)
            record.state = "completed"
            record.progress = 1.0
            record.artifact = artifact
            self.store.save(record)
            self._callback(record, "video.completed")
        except Exception as exc:
            record.state = "failed"
            record.progress = 1.0
            record.error = f"{exc.__class__.__name__}: {exc}"
            self.store.save(record)
            self._callback(record, "video.failed")

    def _callback(self, record: JobRecord, event_type: str) -> None:
        if not self.webhook_client or not record.callback_url:
            return
        try:
            self.webhook_client.deliver(record.callback_url, {
                "type": event_type,
                "created_at": utc_now(),
                "data": record.to_dict(),
            })
        except Exception as exc:
            # Rendering succeeds independently; callback failure is recorded for retry by an operator.
            record.error = (record.error + "; " if record.error else "") + f"callback: {exc}"
            self.store.save(record)

    def wait(self, job_id: str, timeout: float | None = None) -> JobRecord:
        future = self._futures.get(job_id)
        if future:
            future.result(timeout=timeout)
        return self.store.get(job_id)

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=False)

