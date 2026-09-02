import assert from "node:assert/strict";
import { test } from "node:test";

import { ENGINE_REF } from "../lib/config.js";
import {
  ServiceError,
  bearerToken,
  configure,
  dispatch,
  oauthDevice,
  oauthPoll,
  oauthRefresh,
  providerPublicKey,
  repositories,
  runArtifact,
  runStatus,
  submitSteering,
  validateRunRequest,
} from "../lib/service.js";
import { mobileWorkflow } from "../lib/workflow.js";

function queuedFetch(responses) {
  const calls = [];
  const implementation = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    if (!responses.length) throw new Error(`Unexpected fetch: ${url}`);
    const next = responses.shift();
    if (next.throw) throw next.throw;
    const headers = new Headers(next.headers || {});
    const body = next.body === undefined || next.status === 204
      ? null : (Buffer.isBuffer(next.body) ? next.body : JSON.stringify(next.body));
    return new Response(body, { status: next.status ?? 200, headers });
  };
  implementation.calls = calls;
  return implementation;
}

function validRun(overrides = {}) {
  return {
    request_id: "4d32c8e5-6f2b-4a98-a7f5-99594c49b2f8",
    mode: "audit",
    provider: "ollama",
    repository: "owner/project",
    ref: "main",
    file: "",
    goal: "",
    scout_apply: false,
    max_cost: 50,
    threshold: 90,
    max_iterations: 5,
    economy: true,
    use_both: true,
    ...overrides,
  };
}

test("the reusable workflow is pinned to the release that carries this client", () => {
  assert.equal(ENGINE_REF, "android-v3.4.0");
  assert.match(mobileWorkflow(), /mobile-run\.yml@android-v3\.4\.0/);
  for (const mode of ["refactor", "scout", "audit", "prodready"]) {
    assert.match(mobileWorkflow(), new RegExp(mode));
  }
});

test("device sign-in requests rotating tokens and accepts only GitHub's fixed verification URL", async () => {
  const fetcher = queuedFetch([{
    body: {
      device_code: "device-secret",
      user_code: "ABCD-EFGH",
      verification_uri: "https://github.com/login/device",
      expires_in: 900,
      interval: 3,
    },
  }]);
  const result = await oauthDevice(fetcher);
  assert.equal(result.interval, 5);
  assert.equal(result.verification_uri, "https://github.com/login/device");
  const sent = fetcher.calls[0].options.body.toString();
  assert.match(sent, /scope=repo\+workflow\+offline_access/);
});

test("device sign-in rejects a substituted verification host", async () => {
  const fetcher = queuedFetch([{
    body: {
      device_code: "device-secret",
      user_code: "ABCD-EFGH",
      verification_uri: "https://attacker.invalid/device",
      expires_in: 900,
    },
  }]);
  await assert.rejects(() => oauthDevice(fetcher),
    (error) => error instanceof ServiceError && error.code === "oauth_device_invalid");
});

test("device OAuth exchange and refresh rotate tokens without a client secret", async () => {
  const fetcher = queuedFetch([
    { body: { access_token: "gho_access_one", refresh_token: "ghr_refresh_one", expires_in: 28_800 } },
    { body: { access_token: "gho_access_two", refresh_token: "ghr_refresh_two", expires_in: 28_800 } },
  ]);
  const first = await oauthPoll("device-code", fetcher);
  const second = await oauthRefresh(first.refresh_token, fetcher);
  assert.equal(second.access_token, "gho_access_two");
  assert.equal(second.refresh_token, "ghr_refresh_two");
  assert.doesNotMatch(fetcher.calls[0].options.body.toString(), /client_secret/);
  assert.doesNotMatch(fetcher.calls[1].options.body.toString(), /client_secret/);
  assert.match(fetcher.calls[1].options.body.toString(), /grant_type=refresh_token/);
});

test("bearer authentication rejects whitespace and line breaks", () => {
  assert.equal(bearerToken({ authorization: "Bearer gho_valid_token" }), "gho_valid_token");
  for (const value of ["", "token", "Bearer short", "Bearer one two", "Bearer abcdefgh\nInjected: x"]) {
    assert.throws(() => bearerToken({ authorization: value }), ServiceError);
  }
});

test("all four domain requests are accepted and mode-specific invariants fail closed", () => {
  assert.equal(validateRunRequest(validRun({
    mode: "refactor", file: "src/app.js", goal: "Make failures explicit",
  })).mode, "refactor");
  assert.equal(validateRunRequest(validRun({ mode: "scout", scout_apply: true })).mode, "scout");
  assert.equal(validateRunRequest(validRun({ mode: "audit" })).mode, "audit");
  assert.equal(validateRunRequest(validRun({ mode: "prodready" })).mode, "prodready");
  assert.throws(() => validateRunRequest(validRun({ mode: "audit", scout_apply: true })), ServiceError);
  assert.throws(() => validateRunRequest(validRun({ ref: "../main" })), ServiceError);
  assert.throws(() => validateRunRequest(validRun({ max_cost: 151 })), ServiceError);
});

test("repository discovery returns one bounded page of administrable targets", async () => {
  const first = Array.from({ length: 100 }, (_, index) => ({
    full_name: `owner/repo-${index}`,
    default_branch: "main",
    private: index % 2 === 0,
    permissions: { admin: index !== 5 },
  }));
  const fetcher = queuedFetch([{ body: first }]);
  const result = await repositories("gho_repository_token", 7, fetcher);
  assert.equal(result.repositories.length, 99);
  assert.equal(result.page, 7);
  assert.equal(result.has_more, true);
  assert.equal(fetcher.calls.length, 1);
  assert.match(fetcher.calls[0].url, /per_page=100&page=7/);
  assert.equal(result.repositories[0].private, true);
  const lastPage = await repositories("gho_repository_token", 100,
    queuedFetch([{ body: first }]));
  assert.equal(lastPage.has_more, true);
  await assert.rejects(() => repositories("gho_repository_token", 101, fetcher),
    (error) => error instanceof ServiceError && error.code === "invalid_page");
});

test("configuration validates the account, scopes, and an administrable repository", async () => {
  const fetcher = queuedFetch([
    { body: { login: "operator" }, headers: { "x-oauth-scopes": "repo, workflow" } },
    { body: [{ full_name: "operator/project", default_branch: "main", private: true,
      permissions: { admin: true } }] },
  ]);
  assert.deepEqual(await configure("gho_configuration_token", fetcher), { login: "operator" });
});

test("configuration searches bounded pages for an administrable repository", async () => {
  const full = Array.from({ length: 100 }, (_, index) => ({
    full_name: `viewer/repo-${index}`, permissions: { admin: false },
  }));
  const fetcher = queuedFetch([
    { body: { login: "operator" }, headers: { "x-oauth-scopes": "repo, workflow" } },
    { body: full },
    { body: [{ full_name: "operator/project", default_branch: "trunk", private: true,
      permissions: { admin: true } }] },
  ]);
  assert.deepEqual(await configure("gho_configuration_token", fetcher), { login: "operator" });
  assert.match(fetcher.calls[2].url, /page=2/);
});

test("provider public keys are fetched through the managed service", async () => {
  const fetcher = queuedFetch([{ body: { key: "base64-public-key", key_id: "12345" } }]);
  assert.deepEqual(await providerPublicKey("gho_provider_token", "owner/project", fetcher), {
    key: "base64-public-key",
    key_id: "12345",
  });
  assert.match(fetcher.calls[0].url, /actions\/secrets\/public-key$/);
});

test("dispatch uses the default-branch caller and GitHub's authoritative run ID", async () => {
  const expectedWorkflow = mobileWorkflow();
  const fetcher = queuedFetch([
    { status: 200, body: { default_branch: "main" } },
    { status: 200, body: { sha: "workflow-sha", content: Buffer.from(expectedWorkflow).toString("base64") } },
    { status: 200, body: {
      workflow_run_id: 987654,
      run_url: "https://api.github.com/repos/owner/project/actions/runs/987654",
      html_url: "https://github.com/owner/project/actions/runs/987654",
    } },
  ]);
  const state = await dispatch("gho_dispatch_token", validRun({ ref: "feature/release" }),
    {}, fetcher, async () => {});
  assert.equal(state.id, 987654);
  assert.equal(fetcher.calls.length, 3);
  assert.match(fetcher.calls[1].url, /flexfactor-mobile\.yml\?ref=main$/);
  assert.match(fetcher.calls[2].url, /flexfactor-mobile\.yml\/dispatches$/);
  const dispatchBody = JSON.parse(fetcher.calls[2].options.body);
  assert.equal(dispatchBody.ref, "main");
  assert.equal(dispatchBody.inputs.target_ref, "feature/release");
  assert.doesNotMatch(fetcher.calls[2].options.body, /OPENAI|ANTHROPIC|gho_dispatch_token/);
});

test("sealed provider values are forwarded without accepting plaintext key fields", async () => {
  const expectedWorkflow = mobileWorkflow();
  const fetcher = queuedFetch([
    { body: { default_branch: "main" } },
    { body: { sha: "workflow-sha", content: Buffer.from(expectedWorkflow).toString("base64") } },
    { status: 204 },
    { status: 200, body: { workflow_run_id: 12,
      run_url: "https://api.github.com/repos/owner/project/actions/runs/12",
      html_url: "https://github.com/owner/project/actions/runs/12" } },
  ]);
  const sealed = { key_id: "key-123", encrypted_value: Buffer.alloc(64, 7).toString("base64") };
  await dispatch("gho_sealed_token", validRun({ provider: "openai" }),
    { OPENAI_API_KEY: sealed }, fetcher, async () => {});
  const secretWrite = fetcher.calls[2];
  assert.match(secretWrite.url, /actions\/secrets\/OPENAI_API_KEY$/);
  assert.equal(JSON.parse(secretWrite.options.body).encrypted_value, sealed.encrypted_value);
  assert.doesNotMatch(secretWrite.options.body, /sk-|api[_-]?key/i);
});

test("run status reports the active engine step", async () => {
  const fetcher = queuedFetch([
    { body: { id: 99, status: "in_progress", conclusion: null, html_url: "https://example.invalid/run" } },
    { body: { jobs: [{ steps: [{ name: "Build", status: "completed" },
      { name: "Independent review", status: "in_progress" }] }] } },
  ]);
  const result = await runStatus("gho_status_token", "owner/project", 99, fetcher);
  assert.equal(result.step, "Independent review");
});

test("artifact downloads reject a redirect outside GitHub's signed storage", async () => {
  const fetcher = queuedFetch([
    { body: { artifacts: [{ id: 123, name: "mobile-phone-request", expired: false }] } },
    { status: 302, headers: { location: "https://attacker.invalid/result.zip" } },
  ]);
  await assert.rejects(() => runArtifact("gho_artifact_token", "owner/project", 42, fetcher),
    (error) => error instanceof ServiceError && error.code === "untrusted_artifact_location");
});

test("artifact downloads return only a bounded GitHub-signed archive", async () => {
  const archive = Buffer.from("PK\u0003\u0004bounded-test-archive");
  const fetcher = queuedFetch([
    { body: { artifacts: [{ id: 123, name: "mobile-phone-request", expired: false }] } },
    { status: 302, headers: { location: "https://results.blob.core.windows.net/run/result.zip" } },
    { status: 200, body: archive },
  ]);
  assert.deepEqual(await runArtifact("gho_artifact_token", "owner/project", 42, fetcher), archive);
  assert.equal(fetcher.calls[2].options.headers.Authorization, undefined);
});

test("steering uses a bounded repository variable and never reflects the bearer token", async () => {
  const fetcher = queuedFetch([{ status: 404, body: { message: "Not Found" } }, { status: 201, body: {} }]);
  assert.deepEqual(await submitSteering("gho_steering_token", "owner/project",
    "4d32c8e5-6f2b-4a98-a7f5-99594c49b2f8", "Re-run the accessibility checks", fetcher),
  { accepted: true });
  const payload = JSON.parse(fetcher.calls[1].options.body);
  assert.match(payload.name, /^FLEXFACTOR_STEERING_[A-F0-9]{16}$/);
  assert.doesNotMatch(fetcher.calls[1].options.body, /gho_steering_token/);
});

test("upstream errors never include the access token", async () => {
  const token = "gho_never_echo_this_value";
  const fetcher = queuedFetch([{ status: 500, body: { message: "upstream failed" } }]);
  await assert.rejects(() => repositories(token, 1, fetcher), (error) => {
    assert.doesNotMatch(error.message, new RegExp(token));
    return error instanceof ServiceError;
  });
});
