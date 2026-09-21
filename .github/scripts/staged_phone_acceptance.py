#!/usr/bin/env python3
"""Isolated exact-release Android acceptance over a protected localhost TLS relay.

The production APK and its source tree are never edited. Native Vercel CLI owns
deployment protection; application credentials travel through stdin, not argv.
No requests are made to model providers by this tool.
"""
from __future__ import annotations

import argparse
import email.parser
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import tarfile
import time
import urllib.parse

SCOPE = "buckeye7066-7954s-projects"
PACKAGE = "com.firer.console.flexfactor.staged"
TARGET = "buckeye7066/flexfactor-demo-tinystats"
PORT = 18443
ROUTES = {
    ("GET", "/api/health"): set(),
    ("POST", "/api/oauth/device"): set(),
    ("POST", "/api/oauth/token"): set(),
    ("POST", "/api/oauth/refresh"): set(),
    ("POST", "/api/configure"): set(),
    ("GET", "/api/repositories"): {"page"},
    ("POST", "/api/runs/dispatch"): set(),
    ("GET", "/api/runs/status"): {"repository", "request_id", "run_id"},
    ("GET", "/api/runs/details"): {"repository", "request_id", "run_id"},
    ("POST", "/api/runs/steer"): set(),
    ("GET", "/api/provider-key"): {"repository"},
}


def run(command, *, cwd, payload=None, timeout=350):
    environment = dict(os.environ)
    for key in ("DEBUG", "VERCEL_DEBUG", "NODE_DEBUG", "NODE_OPTIONS",
                "FLEXFACTOR_LIVE_PROOF_TOKEN"):
        environment.pop(key, None)
    try:
        result = subprocess.run(command, cwd=cwd, input=payload,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=environment, timeout=timeout, check=False,
                                shell=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Acceptance command failed (details suppressed)") from None
    if result.returncode:
        raise RuntimeError("Acceptance command failed (details suppressed)")
    return result.stdout


def deployment_url(value):
    if not re.fullmatch(
            r"https://flexfactor-cloud-[a-z0-9]+-buckeye7066-7954s-projects\.vercel\.app",
            value):
        raise ValueError("An immutable owner deployment URL is required")
    return value


def native_vercel(repo, arguments, payload=None):
    executable = shutil.which("vercel")
    if not executable:
        raise RuntimeError("Native signed-in Vercel CLI is required")
    command = [executable]
    if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat", ".ps1"):
        node = shutil.which("node")
        entrypoint = Path(executable).parent / "node_modules/vercel/dist/vc.js"
        if not node or not entrypoint.is_file():
            raise RuntimeError("Native Vercel entrypoint not found")
        command = [node, str(entrypoint)]
    return run([*command, *arguments], cwd=repo / "cloud", payload=payload)


def verify_owner(repo, base):
    deployment_url(base)
    linked = json.loads((repo / "cloud/.vercel/project.json").read_text(encoding="utf-8"))
    if (not re.fullmatch(r"team_[A-Za-z0-9]+", str(linked.get("orgId", "")))
            or not re.fullmatch(r"prj_[A-Za-z0-9]+", str(linked.get("projectId", "")))):
        raise ValueError("Invalid linked project identity")
    endpoint = "/v13/deployments/" + base.removeprefix("https://") + "?teamId=" + linked["orgId"]
    meta = json.loads(native_vercel(repo, ["api", endpoint, "--method", "GET",
                                         "--raw", "--scope", SCOPE]))
    if (meta.get("projectId") != linked["projectId"]
            or meta.get("name") != "flexfactor-cloud"
            or meta.get("url") != base.removeprefix("https://")
            or meta.get("readyState") != "READY"
            or meta.get("target") != "production"):
        raise ValueError("Deployment does not match the linked owner project")


def validate_request(method, path, body):
    if not re.fullmatch(r"/api/[A-Za-z0-9_/?=&%.-]+", path):
        raise ValueError("Invalid API path")
    parsed = urllib.parse.urlsplit(path)
    allowed = ROUTES.get((method, parsed.path))
    query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    if allowed is None or set(query) != allowed or any(len(v) != 1 for v in query.values()):
        raise ValueError("Unsupported acceptance route or query")
    if "repository" in query and query["repository"] != [TARGET]:
        raise ValueError("Only the disposable acceptance repository is allowed")
    if body is not None and (not isinstance(body, dict) or method != "POST"):
        raise ValueError("A JSON object is required")
    if method == "POST" and body is None:
        raise ValueError("A JSON body is required")
    if parsed.path == "/api/runs/dispatch":
        request = body.get("request")
        if (not isinstance(request, dict) or request.get("repository") != TARGET
                or body.get("encrypted_secrets") != {}):
            raise ValueError("Dispatch requires disposable target and no provider keys")
    if parsed.path == "/api/runs/steer" and body.get("repository") != TARGET:
        raise ValueError("Steering requires the disposable target")
    return parsed.path


def parse_response(raw):
    while True:
        block, separator, payload = raw.partition(b"\r\n\r\n")
        status_line, _, header_bytes = block.partition(b"\r\n")
        match = re.fullmatch(rb"HTTP/[12](?:\.[01])? ([0-9]{3})(?: [^\r\n]*)?", status_line)
        if not separator or not match:
            raise ValueError("Upstream response is not HTTP")
        status = int(match[1])
        if status == 100:
            raw = payload
            continue
        headers = email.parser.BytesHeaderParser().parsebytes(header_bytes)
        return status, payload, headers


def forward(repo, base, method, path, headers, body=None):
    deployment_url(base)
    validate_request(method, path, body)

    def quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\r', '\\r').replace('\n', '\\n') + '"'

    config = ["silent", "show-error", "include", "suppress-connect-headers",
              "no-location", "no-location-trusted", "max-redirs = 0",
              "max-time = 330", "request = " + quote(method)]
    allowed_headers = {"authorization", "accept", "content-type", "user-agent",
                       "x-flexfactor-client-version"}
    for key, value in headers.items():
        if key.lower() not in allowed_headers or '\r' in value or '\n' in value:
            raise ValueError("Unsupported request header")
        config.append("header = " + quote(key + ": " + value))
    if body is not None:
        config.append("data-binary = " + quote(json.dumps(body)))
    raw = native_vercel(repo, ["curl", path, "--deployment", base, "--scope", SCOPE,
                              "--", "--disable", "--config", "-"],
                        ("\n".join(config) + "\n").encode())
    return parse_response(raw)


def prepare(repo, output, version, source_sha, openssl):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Exact release version and source SHA are required")
    tag = "refs/tags/android-v" + version
    actual = run(["git", "-C", str(repo), "rev-parse", tag + "^{commit}"], cwd=repo).decode().strip()
    if actual != source_sha:
        raise ValueError("Release tag differs from authorized source SHA")
    if output.exists():
        raise ValueError("Use a fresh output directory; existing evidence is never replaced")
    output.mkdir(parents=True)
    archive = run(["git", "-C", str(repo), "archive", source_sha, "android"], cwd=repo)
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        contents.extractall(output, filter="data")
    root = output / "android"
    before = {str(p.relative_to(root)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob("*") if p.is_file()}
    tls = output / "tls"
    tls.mkdir()
    (tls / "openssl.cnf").write_text("[req]\ndistinguished_name=dn\n[dn]\n", encoding="ascii")
    # A fresh short-lived certificate is trusted only by this test APK on localhost.
    # Never print or commit the generated private key; it is used solely by ssl.
    run([openssl, "req", "-config", str(tls / "openssl.cnf"), "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-keyout", str(tls / "localhost.key"), "-out", str(tls / "localhost.pem"),
         "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
         "-addext", "basicConstraints=critical,CA:TRUE"], cwd=output)
    build = root / "app/build.gradle.kts"
    source = build.read_text(encoding="utf-8")
    replacements = {
        'applicationId = "com.firer.console.flexfactor"': 'applicationId = "' + PACKAGE + '"',
        'https://flexfactor-cloud.vercel.app': 'https://localhost:' + str(PORT),
    }
    for old, new in replacements.items():
        if source.count(old) != 1:
            raise ValueError("Released Android build layout changed")
        source = source.replace(old, new)
    build.write_text(source, encoding="utf-8", newline="\n")
    manifest = root / "app/src/main/AndroidManifest.xml"
    text = manifest.read_text(encoding="utf-8")
    if text.count('android:label="FlexFactor"') != 1:
        raise ValueError("Released Android manifest layout changed")
    manifest.write_text(text.replace('android:label="FlexFactor"', 'android:label="FlexFactor Staged"'),
                        encoding="utf-8", newline="\n")
    resources = root / "app/src/main/res"
    (resources / "raw").mkdir(exist_ok=True)
    shutil.copyfile(tls / "localhost.pem", resources / "raw/staged_localhost.pem")
    (resources / "xml/network_security_config.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<network-security-config>\n'
        '  <base-config cleartextTrafficPermitted="false" />\n'
        '  <domain-config cleartextTrafficPermitted="false">\n'
        '    <domain includeSubdomains="false">localhost</domain>\n'
        '    <trust-anchors><certificates src="@raw/staged_localhost" /></trust-anchors>\n'
        '  </domain-config>\n'
        '</network-security-config>\n', encoding="utf-8")
    after = {str(p.relative_to(root)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.rglob("*") if p.is_file()}
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    expected = ["app/build.gradle.kts", "app/src/main/AndroidManifest.xml",
                "app/src/main/res/raw/staged_localhost.pem",
                "app/src/main/res/xml/network_security_config.xml"]
    if changed != expected:
        raise ValueError("Unexpected release overlay files")
    evidence = {"schema": "flexfactor-staged-phone-v1", "release_source_sha": source_sha,
                "release_tag": tag, "version": version, "package": PACKAGE,
                "cloud_endpoint": "https://localhost:" + str(PORT), "overlay_files": changed,
                "original_sha256": before, "overlay_sha256": after,
                "certificate_sha256": hashlib.sha256((tls / "localhost.pem").read_bytes()).hexdigest(),
                "production_release_identity_unchanged": True}
    (output / "provenance.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return evidence


def relay(repo, output, base, source_sha, engine_ref):
    verify_owner(repo, base)
    status, raw, headers = forward(repo, base, "GET", "/api/health", {})
    health = json.loads(raw)
    if (status != 200 or health.get("ok") is not True
            or health.get("oauth_device_configured") is not True
            or health.get("source_revision") != source_sha
            or health.get("engine_ref") != engine_ref
            or health.get("deployment_url") != base):
        raise ValueError("Staged cloud health differs from authorized identity")
    identity_headers = {"X-FlexFactor-Cloud-Source": source_sha, "X-FlexFactor-Deployment": base}
    if any(headers.get(k) != v for k, v in identity_headers.items()):
        raise ValueError("Staged health identity headers are missing")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log URLs, OAuth bodies, Authorization, or upstream stderr.

        def do_GET(self):
            self.request_cloud()

        def do_POST(self):
            self.request_cloud()

        def request_cloud(self):
            route = "rejected"
            try:
                if self.headers.get("Host") != "localhost:" + str(PORT):
                    raise ValueError("Wrong relay host")
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Chunked requests are not supported")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 262144:
                    raise ValueError("Request size exceeded")
                body = json.loads(self.rfile.read(size)) if size else None
                route = validate_request(self.command, self.path, body)
                request_headers = {key: self.headers[key] for key in (
                    "Authorization", "Accept", "Content-Type", "User-Agent",
                    "X-FlexFactor-Client-Version") if self.headers.get(key) is not None}
                code, payload, response_headers = forward(repo, base, self.command,
                                                         self.path, request_headers, body)
                if any(response_headers.get(k) != v for k, v in identity_headers.items()):
                    raise RuntimeError("Upstream cloud identity changed")
            except (ValueError, RuntimeError, OSError):
                code, payload, response_headers = 502, b'{"message":"Staged acceptance relay refused request"}', {}
            self.send_response(code)
            self.send_header("Content-Type", response_headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            for key in identity_headers:
                if response_headers.get(key):
                    self.send_header(key, response_headers[key])
            self.end_headers()
            self.wfile.write(payload)
            record = {"time": int(time.time()), "method": self.command, "route": route,
                      "status": code, "deployment": base, "source_revision": source_sha}
            with (output / "relay-evidence.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(record) + "\n")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(output / "tls/localhost.pem", output / "tls/localhost.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server, health


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "serve"])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument("--release-sha")
    parser.add_argument("--openssl", default="openssl")
    parser.add_argument("--deployment")
    parser.add_argument("--cloud-source-sha")
    parser.add_argument("--engine-ref")
    args = parser.parse_args()
    repo, output = args.repo.resolve(), args.output.resolve()
    if args.action == "prepare":
        result = prepare(repo, output, args.version or "", args.release_sha or "", args.openssl)
        print(json.dumps({k: result[k] for k in ("release_source_sha", "package", "overlay_files")}))
    else:
        if not re.fullmatch(r"[0-9a-f]{40}", args.cloud_source_sha or ""):
            parser.error("serve requires an exact --cloud-source-sha")
        if not re.fullmatch(r"android-v[0-9]+\.[0-9]+\.[0-9]+", args.engine_ref or ""):
            parser.error("serve requires an exact --engine-ref")
        server, health = relay(repo, output, args.deployment or "", args.cloud_source_sha, args.engine_ref)
        print(json.dumps({"listening": "https://localhost:" + str(PORT), "health": health}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
