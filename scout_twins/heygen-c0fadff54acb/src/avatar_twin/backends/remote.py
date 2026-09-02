from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
import hashlib
import http.client
import json
import os
import time

from .base import AvatarRenderRequest, BackendArtifact
from ..configuration import ProviderSpec
from ..models import ValidationError


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class RemoteWorkerBackend:
    """Thin-client adapter for the documented Scout GPU worker protocol."""

    name = "remote_worker"
    model = "operator-configured-remote-avatar-worker"

    def __init__(self, spec: ProviderSpec, *, max_download_bytes: int) -> None:
        self.spec = spec
        self.max_download_bytes = max_download_bytes
        self.endpoint = str(spec.options.get("endpoint") or "").rstrip("/") + "/"
        self.token_env = str(spec.options.get("token_env") or "AVATAR_TWIN_WORKER_TOKEN")

    def _allowed_hosts(self) -> set[str]:
        host = (urlsplit(self.endpoint).hostname or "").lower()
        extra = {str(item).lower() for item in self.spec.options.get("allowed_hosts") or []}
        return {host, *extra} - {""}

    def _url(self, value: str) -> str:
        url = urljoin(self.endpoint, value)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValidationError("remote worker URLs must use HTTPS")
        if parsed.hostname.lower() not in self._allowed_hosts():
            raise ValidationError(f"remote worker returned a URL for an unapproved host: {parsed.hostname}")
        return url

    def _headers(self) -> dict[str, str]:
        token = os.environ.get(self.token_env, "").strip()
        if not token:
            raise ValidationError(f"remote worker credential is missing from {self.token_env}")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _json(self, url: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = self._headers()
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self._url(url), data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=min(self.spec.timeout_s, 120.0)) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raise RuntimeError(f"remote worker returned HTTP {exc.code} for {method}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"remote worker request failed: {exc}") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("remote worker JSON response exceeded 2 MiB")
        try:
            value = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("remote worker returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("remote worker returned an invalid response shape")
        return value

    def _put_file(self, url: str, source: Path) -> None:
        target = urlsplit(self._url(url))
        request_path = target.path or "/"
        if target.query:
            request_path += "?" + target.query
        connection = http.client.HTTPSConnection(target.hostname, target.port, timeout=180)
        try:
            connection.putrequest("PUT", request_path)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(source.stat().st_size))
            connection.putheader("X-Content-SHA256", _hash(source))
            connection.endheaders()
            with source.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    connection.send(block)
            response = connection.getresponse()
            response.read()
            if response.status // 100 != 2:
                raise RuntimeError(f"remote asset upload returned HTTP {response.status}")
        finally:
            connection.close()

    def _download(self, url: str, destination: Path) -> None:
        artifact_url = self._url(url)
        endpoint_host = (urlsplit(self.endpoint).hostname or "").lower()
        artifact_host = (urlsplit(artifact_url).hostname or "").lower()
        # Presigned object-storage URLs are already credentials. Never forward
        # the worker bearer token to an allowed-but-separate artifact host.
        headers = self._headers() if artifact_host == endpoint_host else {"Accept": "application/octet-stream"}
        request = Request(artifact_url, headers=headers, method="GET")
        temporary = destination.with_suffix(".download")
        total = 0
        try:
            with urlopen(request, timeout=180) as response, temporary.open("wb") as stream:
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > self.max_download_bytes:
                        raise RuntimeError("remote render exceeded max_download_bytes")
                    stream.write(block)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        if total < 1:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("remote worker returned an empty artifact")
        temporary.replace(destination)

    def readiness(self) -> dict[str, Any]:
        try:
            parsed = urlsplit(self._url("health"))
            configured = bool(os.environ.get(self.token_env, "").strip())
            return {
                "ready": configured,
                "provider": self.name,
                "endpoint_host": parsed.hostname,
                "credential_env": self.token_env,
                "reason": "" if configured else f"{self.token_env} is not set",
            }
        except Exception as exc:
            return {"ready": False, "provider": self.name, "reason": str(exc)}

    def render(self, request: AvatarRenderRequest) -> BackendArtifact:
        job = request.validated()
        inputs = {
            "avatar_image": {"bytes": job.avatar_image.stat().st_size, "sha256": _hash(job.avatar_image)},
            "audio": {"bytes": job.audio.stat().st_size, "sha256": _hash(job.audio)},
        }
        if job.driving_video:
            inputs["driving_video"] = {
                "bytes": job.driving_video.stat().st_size,
                "sha256": _hash(job.driving_video),
            }
        created = self._json("jobs", method="POST", payload={
            "client_job_id": job.job_id,
            "mode": job.mode,
            "prompt": job.prompt,
            "expected_duration_s": job.expected_duration_s,
            "inputs": inputs,
            "provider": str(self.spec.options.get("worker_provider") or ""),
        })
        remote_job_id = str(created.get("job_id") or "")
        uploads = created.get("uploads") or {}
        if not remote_job_id or not isinstance(uploads, dict):
            raise RuntimeError("remote worker did not return job_id and upload URLs")
        sources = {"avatar_image": job.avatar_image, "audio": job.audio}
        if job.driving_video:
            sources["driving_video"] = job.driving_video
        for name, source in sources.items():
            upload_url = str(uploads.get(name) or "")
            if not upload_url:
                raise RuntimeError(f"remote worker omitted the {name} upload URL")
            self._put_file(upload_url, source)
        start_url = str(created.get("start_url") or f"jobs/{remote_job_id}/run")
        self._json(start_url, method="POST", payload={})
        status_url = str(created.get("status_url") or f"jobs/{remote_job_id}")
        deadline = time.monotonic() + self.spec.timeout_s
        poll_s = max(0.5, min(30.0, float(self.spec.options.get("poll_interval_s", 3.0))))
        final: dict[str, Any] = {}
        while time.monotonic() < deadline:
            final = self._json(status_url)
            state = str(final.get("status") or "").lower()
            if state == "completed":
                break
            if state == "failed":
                raise RuntimeError(f"remote worker job failed: {final.get('error') or 'no detail'}")
            time.sleep(poll_s)
        else:
            raise RuntimeError(f"remote worker job exceeded its {self.spec.timeout_s:.0f}s timeout")
        artifact_url = str(final.get("artifact_url") or "")
        if not artifact_url:
            raise RuntimeError("remote worker completed without an artifact URL")
        destination = job.output_dir / "model-video.mp4"
        self._download(artifact_url, destination)
        receipt = {
            "remote_job_id": remote_job_id,
            "endpoint_host": urlsplit(self.endpoint).hostname,
            "status": "completed",
            "provider": final.get("provider"),
            "model": final.get("model"),
            "worker_receipt": final.get("receipt"),
        }
        return BackendArtifact(
            provider=self.name,
            model=str(final.get("model") or self.model),
            video_path=destination,
            receipts=(receipt,),
            metadata={"mode": job.mode, "remote_job_id": remote_job_id},
        )
