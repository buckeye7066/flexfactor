# Remote GPU worker contract

`remote_worker` keeps model weights and GPU memory outside this Git branch and the desktop process. The client accepts only HTTPS (loopback is not accepted for production worker URLs), pins returned URLs to configured hosts, streams files in 1 MiB chunks, limits downloads, and does not send the worker bearer token to a separate presigned artifact host.

## Endpoints

All control endpoints use `Authorization: Bearer $AVATAR_TWIN_WORKER_TOKEN`.

### `GET /v1/health`

Return HTTP 200 when the worker can accept a job. This route is used for operator readiness; the client also requires the token to be configured.

### `POST /v1/jobs`

Request:

```json
{
  "client_job_id": "project-id",
  "mode": "talking_avatar or performance_transfer",
  "prompt": "creative direction",
  "expected_duration_s": 12.4,
  "provider": "optional worker provider",
  "inputs": {
    "avatar_image": {"bytes": 123, "sha256": "..."},
    "audio": {"bytes": 456, "sha256": "..."},
    "driving_video": {"bytes": 789, "sha256": "..."}
  }
}
```

Response:

```json
{
  "job_id": "worker-job-id",
  "uploads": {
    "avatar_image": "https://approved-host/presigned-upload",
    "audio": "https://approved-host/presigned-upload",
    "driving_video": "https://approved-host/presigned-upload"
  },
  "start_url": "/v1/jobs/worker-job-id/run",
  "status_url": "/v1/jobs/worker-job-id"
}
```

The client uploads each exact asset with `Content-Length` and `X-Content-SHA256`.

### `POST /v1/jobs/{job_id}/run`

Start the selected real model. Return a JSON object; no success artifact is expected yet.

### `GET /v1/jobs/{job_id}`

While queued/running, return `{"status":"queued"}` or `{"status":"running"}`. On failure, return `{"status":"failed","error":"..."}`. On completion:

```json
{
  "status": "completed",
  "provider": "wan_animate",
  "model": "Wan-AI/Wan2.2-Animate-14B",
  "artifact_url": "https://approved-host/presigned-result",
  "receipt": {"worker_job_id": "...", "model_revision": "..."}
}
```

The result must be a new video, but the worker's claim is not trusted by itself. The client downloads it under the configured byte ceiling, runs FFprobe, checks dimensions/frames/duration, samples temporal motion, rejects an unchanged driving video, then performs authoritative audio/composition assembly locally.
