#!/usr/bin/env python3
"""Live, redacted proof of the released Android cloud contract."""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from mobile_release_identity import (
    verify_release_identity, verify_cloud_health, verify_cloud_response,
)


def build_live_request(request_id, repository, client_version, mode):
    """Exercise only the designated disposable target, with a one-dollar cap."""
    if repository != 'buckeye7066/flexfactor-demo-tinystats':
        raise ValueError('Live acceptance is restricted to the disposable repository')
    if mode not in ('refactor', 'scout', 'audit', 'prodready'):
        raise ValueError('Unsupported live acceptance mode')
    guidance = (f'Live {client_version} acceptance proof; do not apply proposed changes.'
                if mode == 'scout' else
                f'Live {client_version} acceptance: apply verified defect repairs only, '
                'preserve documented behavior, run the project tests, and publish only through the normal verification gates.')
    goal = ('Correct mean to divide by the number of values; use a fresh default bucket '
            'in append_item; make parse_port enforce 1-65535 and raise ValueError on invalid input. '
            'Preserve the documented APIs and do not weaken validation or tests.')
    return {'request_id': request_id, 'mode': mode, 'provider': 'auto',
            'repository': repository, 'ref': 'main',
            'file': 'stats_utils.py' if mode == 'refactor' else '',
            'goal': goal if mode == 'refactor' else '', 'guidance': guidance,
            'scout_apply': False, 'max_cost': 1, 'threshold': 90, 'max_iterations': 1}


def validate_live_result(result, request, run_id):
    """Require the exact correlated result and truthful publication evidence."""
    if not isinstance(result, dict):
        raise ValueError('The phone result is not a structured object')
    expected = {'request_id': request['request_id'], 'mode': request['mode'],
                'target_repository': request['repository'], 'target_ref': request['ref'],
                'run_url': f"https://github.com/{request['repository']}/actions/runs/{run_id}"}
    if any(result.get(key) != value for key, value in expected.items()):
        raise ValueError('The phone result does not identify the requested run')
    if (result.get('success') is not True or type(result.get('exit_code')) is not int
            or result['exit_code'] != 0 or result.get('publication_complete') is not True):
        raise ValueError('The phone result does not prove completed execution and publication')
    before, after = result.get('source_before'), result.get('source_after')
    if any(not isinstance(sha, str) or len(sha) != 40
           or any(ch not in '0123456789abcdef' for ch in sha) for sha in (before, after)):
        raise ValueError('The phone result omitted valid source revisions')
    if request['mode'] == 'refactor' and before == after:
        raise ValueError('Refactor acceptance requires a verified published source change')
    if result.get('publication_required') is not (before != after):
        raise ValueError('The phone result contradicts its source-change evidence')


def record_live_result(result, request, run_id, terminal, save):
    """An inspected artifact is not a pass until every acceptance check succeeds."""
    try:
        if terminal.get('conclusion') != 'success':
            raise ValueError('The target run did not conclude successfully')
        validate_live_result(result, request, run_id)
    except ValueError as error:
        save(stage='validation-failed', result_validated=False,
             phone_result_present=True, validation_error=str(error))
        raise
    save(stage='artifact-validated', result_validated=True, phone_result_present=True,
         phone_result_success=True, phone_result_mode=result['mode'],
         phone_result_exit_code=result['exit_code'],
         phone_result_publication_required=result['publication_required'],
         phone_result_publication_complete=result['publication_complete'],
         source_before=result['source_before'], source_after=result['source_after'])


BASE = "https://flexfactor-cloud.vercel.app"
TOKEN = os.environ["FLEXFACTOR_LIVE_PROOF_TOKEN"].strip()
REPOSITORY = os.environ["TARGET_REPOSITORY"]
REQUEST_ID = str(uuid.uuid4())
MODE = os.environ.get("MODE", "scout")
ANDROID_BUILD = Path("android/app/build.gradle.kts")
VERSION_MATCH = re.search(
    r'^\s*versionName\s*=\s*"([^"]+)"',
    ANDROID_BUILD.read_text(encoding="utf-8"),
    re.MULTILINE,
)
if VERSION_MATCH is None:
    raise SystemExit("Android versionName is missing from the authorized source")
SOURCE_VERSION = VERSION_MATCH.group(1)
UPDATE_MANIFEST = (
    "https://github.com/buckeye7066/flexfactor/releases/latest/download/"
    "android-update.json"
)
with urllib.request.urlopen(UPDATE_MANIFEST, timeout=30) as response:
    released = json.load(response)
if (released.get("schema") != "flexfactor-update-v1"
        or released.get("channel") != "stable"
        or released.get("status") != "active"):
    raise SystemExit("The public Android release manifest is not active stable v1")
CLIENT_VERSION = str(released.get("versionName", ""))
if CLIENT_VERSION != SOURCE_VERSION:
    raise SystemExit("The authorized source is not the published Android client")

identity = verify_release_identity(
    Path.cwd(), os.environ["EXPECTED_SHA"], released, SOURCE_VERSION)
with urllib.request.urlopen(BASE + "/api/health", timeout=30) as response:
    deployed_health = json.load(response)
    deployed_headers = response.headers
verify_cloud_health(deployed_health, identity)
verify_cloud_response(deployed_headers, deployed_health)
HEADERS = {
    "Accept": "application/json, application/zip",
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "FlexFactor-Mobile-Live-Proof",
    "X-FlexFactor-Client-Version": CLIENT_VERSION,
}

proof = {
    "source_sha": identity["release_source_sha"],
    "verification_sha": identity["verification_sha"],
    "cloud_version": identity["cloud_version"],
    "engine_ref": identity["engine_ref"],
    "cloud_source_sha": identity["cloud_source_sha"],
    "cloud_deployment_url": deployed_health["deployment_url"],
    "client_version": CLIENT_VERSION,
    "target_repository": REPOSITORY,
    "request_id": REQUEST_ID,
    "stage": "initialized",
    "mode": MODE,
}


def save_proof(**updates: object) -> None:
    """Persist only the allow-listed, non-secret acceptance evidence."""
    proof.update(updates)
    Path("mobile-cloud-live-proof.json").write_text(
        json.dumps(proof, indent=2) + "\n", encoding="utf-8")


save_proof()


def request(method: str, path: str, body: object | None = None) -> tuple[int, bytes, str]:
    payload = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=payload, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=330) as response:
            verify_cloud_response(response.headers, deployed_health)
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"{method} {path} returned HTTP {error.code}: {detail}") from None


def json_request(method: str, path: str, body: object | None = None) -> dict:
    status, raw, _ = request(method, path, body)
    if not 200 <= status < 300:
        raise RuntimeError(f"{method} {path} returned HTTP {status}")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {path} returned non-object JSON")
    return value


configured = json_request("POST", "/api/configure", {})
if not configured.get("login"):
    raise SystemExit("Cloud did not identify the live proof account")
save_proof(stage="configured")

target_visible = False
for page in range(1, 101):
    repositories = json_request("GET", f"/api/repositories?page={page}")
    visible = {row.get("full_name") for row in repositories.get("repositories", [])}
    if REPOSITORY in visible:
        target_visible = True
        break
    if not repositories.get("has_more", False):
        break
if not target_visible:
    raise SystemExit("Live proof target was not returned by repository discovery")
save_proof(stage="repository-discovered")

run_request = build_live_request(REQUEST_ID, REPOSITORY, CLIENT_VERSION, MODE)

dispatch_body = {"request": run_request, "encrypted_secrets": {}}
started = json_request("POST", "/api/runs/dispatch", dispatch_body)
run_id = int(started.get("id", 0))
if run_id <= 0:
    raise SystemExit("Dispatch returned no authoritative run ID")
save_proof(stage="dispatched", run_id=run_id)

# Repeating the exact request models process loss after GitHub accepted it.
# GitHub may briefly return the run before its evaluated run-name/path fields
# are consistent. That is a recoverable correlation lag, never permission to
# create a second UUID or dispatch.
recovered = None
for attempt in range(30):
    try:
        recovered = json_request("POST", "/api/runs/dispatch", dispatch_body)
        break
    except RuntimeError as error:
        if "HTTP 409" not in str(error) or attempt == 29:
            raise
        time.sleep(1)
if recovered is None or int(recovered.get("id", 0)) != run_id:
    raise SystemExit("Crash recovery dispatched a duplicate run")
save_proof(stage="dispatch-recovered", dispatch_recovered_same_run=True)

steering = json_request("POST", "/api/runs/steer", {
    "repository": REPOSITORY,
    "request_id": REQUEST_ID,
    "comment": run_request["guidance"],
})
if steering.get("accepted") is not True:
    raise SystemExit("Active steering was not accepted")
save_proof(stage="steering-accepted", steering_accepted=True)

encoded_repo = urllib.parse.quote(REPOSITORY, safe="")
encoded_request = urllib.parse.quote(REQUEST_ID, safe="")
status_path = (f"/api/runs/status?repository={encoded_repo}"
               f"&request_id={encoded_request}&run_id={run_id}")
terminal = None
for _ in range(720):
    state = json_request("GET", status_path)
    if int(state.get("id", 0)) != run_id:
        raise SystemExit("Status returned a different run ID")
    if state.get("status") == "completed":
        terminal = state
        break
    time.sleep(30)
if terminal is None:
    raise SystemExit("Live mobile run did not complete within six hours")
save_proof(
    stage="run-completed",
    terminal_status=terminal.get("status"),
    terminal_conclusion=terminal.get("conclusion", ""),
)

details_path = (f"/api/runs/details?repository={encoded_repo}"
                f"&request_id={encoded_request}&run_id={run_id}")
_, archive, content_type = request("GET", details_path)
if not content_type.lower().startswith("application/zip"):
    raise SystemExit("Details endpoint did not return the phone artifact")
with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
    names = {Path(name).name for name in bundle.namelist()}
    if "mobile-result.json" not in names:
        raise SystemExit("Phone-readable result is missing")
    result_name = next(name for name in bundle.namelist()
                       if Path(name).name == "mobile-result.json")
    result = json.loads(bundle.read(result_name))
    log_names = [name for name in bundle.namelist()
                 if Path(name).name == "mobile-run.log"]
    diagnostic = ""
    if log_names:
        diagnostic = bundle.read(log_names[0]).decode("utf-8", "replace")[-4000:]
        diagnostic = diagnostic.replace(TOKEN, "[REDACTED]")
        diagnostic = re.sub(
            r"(?i)(token|secret|password|authorization|api[_-]?key)\s*[:=]\s*\S+",
            r"\1=[REDACTED]", diagnostic)

record_live_result(result, run_request, run_id, terminal, save_proof)
print(json.dumps(proof))
