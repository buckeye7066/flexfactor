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
    provider: "auto",
    repository: "owner/project",
    ref: "main",
    file: "",
    goal: "",
    scout_source: "",
    scout_apply: false,
    max_cost: 50,
    threshold: 90,
    max_iterations: 6,
    ...overrides,
  };
}

function resolvedRef() {
  return { body: { sha: "a".repeat(40) } };
}

function missingRequestClaim() {
  return { status: 404, body: { message: "Not Found" } };
}

function createdRequestClaim() {
  return { status: 201, body: {} };
}

function markedRequestClaim() {
  return { status: 204 };
}

function installedWorkflow() {
  return {
    status: 200,
    body: {
      sha: "workflow-sha",
      content: Buffer.from(mobileWorkflow()).toString("base64"),
    },
  };
}

function acceptedDispatch(id) {
  return {
    status: 200,
    body: {
      workflow_run_id: id,
      run_url: `https://api.github.com/repos/owner/project/actions/runs/${id}`,
      html_url: `https://github.com/owner/project/actions/runs/${id}`,
    },
  };
}

function storedClaim(request, overrides = {}) {
  return {
    body: {
      name: "FLEXFACTOR_RUN_4D32C8E56F2B4A98A7F5",
      value: JSON.stringify({
        schema: 1,
        request_id: request.request_id,
        state: "claimed",
        run_id: 0,
        ephemeral_secrets: [],
        created_at: "2026-09-02T00:00:00.000Z",
        ...overrides,
      }),
    },
  };
}

test("the reusable workflow is pinned to the release that carries this client", () => {
  assert.equal(ENGINE_REF, "android-v3.6.0");
  assert.match(mobileWorkflow(), /mobile-run\.yml@android-v3\.6\.0/);
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
  const scout = validateRunRequest(validRun({
    mode: "scout", scout_source: "https://source.example/features", scout_apply: true,
  }));
  assert.equal(scout.mode, "scout");
  assert.equal(scout.scout_source, "https://source.example/features");
  assert.equal(validateRunRequest(validRun({ mode: "audit" })).mode, "audit");
  assert.equal(validateRunRequest(validRun({ mode: "prodready" })).mode, "prodready");
  assert.throws(() => validateRunRequest(validRun({ mode: "audit", scout_apply: true })), ServiceError);
  assert.throws(() => validateRunRequest(validRun({ mode: "scout" })), ServiceError);
  assert.throws(() => validateRunRequest(validRun({ mode: "scout", scout_source: "SourceSuite" })), ServiceError);
  assert.throws(() => validateRunRequest(validRun({
    mode: "scout", scout_source: "https://github.com/example/source-suite",
  })), ServiceError);
  assert.equal(validateRunRequest(validRun({
    mode: "scout", scout_source: "https://github.com/features/actions",
  })).scout_source, "https://github.com/features/actions");
  assert.throws(() => validateRunRequest(validRun({ mode: "audit", scout_source: "SourceSuite" })), ServiceError);
  assert.throws(() => validateRunRequest(validRun({ ref: "../main" })), ServiceError);
  assert.throws(() => validateRunRequest(validRun({ max_cost: 151 })), ServiceError);
  assert.throws(() => validateRunRequest(validRun({ provider: "openai" })), ServiceError);
  assert.throws(() => validateRunRequest({ ...validRun(), economy: true }), ServiceError);
  assert.throws(() => validateRunRequest({ ...validRun(), use_both: true }), ServiceError);
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

test("configuration validates the account and scopes without a repository scan", async () => {
  const fetcher = queuedFetch([
    { body: { login: "operator" }, headers: { "x-oauth-scopes": "repo, workflow" } },
  ]);
  assert.deepEqual(await configure("gho_configuration_token", fetcher), { login: "operator" });
  assert.equal(fetcher.calls.length, 1);
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
  const fetcher = queuedFetch([
    { body: { workflow_runs: [] } },
    { status: 200, body: { default_branch: "main" } },
    resolvedRef(),
    missingRequestClaim(),
    createdRequestClaim(),
    installedWorkflow(),
    acceptedDispatch(987654),
    markedRequestClaim(),
  ]);
  const state = await dispatch("gho_dispatch_token", validRun({ ref: "feature/release" }),
    {}, fetcher, async () => {});
  assert.equal(state.id, 987654);
  assert.equal(fetcher.calls.length, 8);
  assert.match(fetcher.calls[0].url, /actions\/runs\?event=workflow_dispatch/);
  assert.doesNotMatch(fetcher.calls[0].url, /branch=|flexfactor-mobile\.yml/);
  assert.match(fetcher.calls[2].url, /commits\/feature%2Frelease$/);
  assert.match(fetcher.calls[3].url, /actions\/variables\/FLEXFACTOR_RUN_/);
  assert.equal(fetcher.calls[4].options.method, "POST");
  assert.match(fetcher.calls[5].url, /flexfactor-mobile\.yml\?ref=main$/);
  assert.match(fetcher.calls[6].url, /flexfactor-mobile\.yml\/dispatches$/);
  const dispatchBody = JSON.parse(fetcher.calls[6].options.body);
  assert.equal(dispatchBody.ref, "main");
  assert.equal(dispatchBody.inputs.target_ref, "feature/release");
  assert.equal(dispatchBody.return_run_details, true);
  assert.doesNotMatch(fetcher.calls[6].options.body, /OPENAI|ANTHROPIC|gho_dispatch_token/);
  const marked = JSON.parse(fetcher.calls[7].options.body);
  assert.equal(JSON.parse(marked.value).run_id, 987654);
});

test("dispatch request IDs are idempotent across phone crash recovery", async () => {
  const request = validRun({ ref: "deleted-after-dispatch" });
  const fetcher = queuedFetch([
    { body: { workflow_runs: [{
      id: 444,
      status: "in_progress",
      conclusion: null,
      display_title: `FlexFactor audit · ${request.request_id}`,
      html_url: "https://github.com/owner/project/actions/runs/444",
    }] } },
  ]);

  const state = await dispatch("gho_idempotent_token", request, {}, fetcher, async () => {});

  assert.equal(state.id, 444);
  assert.equal(state.status, "in_progress");
  assert.equal(fetcher.calls.length, 1);
  assert.equal(fetcher.calls.some((call) => /commits\/deleted-after-dispatch/.test(call.url)), false);
  assert.equal(fetcher.calls.some((call) => call.options.method === "POST"), false);
  assert.equal(fetcher.calls.some((call) => call.options.method === "PUT"), false);
});

test("idempotency recovery follows GitHub pagination without dispatching twice", async () => {
  const request = validRun();
  const fetcher = queuedFetch([
    { headers: { link: '<https://api.github.com/next>; rel="next"' },
      body: { workflow_runs: [] } },
    { body: { workflow_runs: [{
      id: 445,
      status: "completed",
      conclusion: "success",
      display_title: `FlexFactor audit · ${request.request_id}`,
      html_url: "https://github.com/owner/project/actions/runs/445",
    }] } },
  ]);

  const state = await dispatch("gho_idempotent_token", request, {}, fetcher, async () => {});

  assert.equal(state.id, 445);
  assert.equal(fetcher.calls.length, 2);
  assert.match(fetcher.calls[0].url, /per_page=100&page=1$/);
  assert.match(fetcher.calls[1].url, /per_page=100&page=2$/);
  assert.doesNotMatch(fetcher.calls[0].url, /branch=|flexfactor-mobile\.yml/);
  assert.equal(fetcher.calls.some((call) => call.options.method === "POST"), false);
  assert.equal(fetcher.calls.some((call) => call.options.method === "PUT"), false);
});

test("idempotency history failure blocks every dispatch mutation", async () => {
  const fetcher = queuedFetch([
    { status: 404, body: { message: "Not Found" } },
  ]);

  await assert.rejects(
    () => dispatch("gho_history_failure", validRun(), {}, fetcher, async () => {}),
    (error) => error instanceof ServiceError && error.code === "github_request_failed",
  );
  assert.equal(fetcher.calls.length, 1);
  assert.equal(fetcher.calls.some((call) => call.options.method === "POST"), false);
  assert.equal(fetcher.calls.some((call) => call.options.method === "PUT"), false);
});

test("idempotency scan stops at GitHub's ten-page filtered-run ceiling", async () => {
  const pages = Array.from({ length: 10 }, () => ({
    headers: { link: '<https://api.github.com/next>; rel="next"' },
    body: { workflow_runs: [] },
  }));
  const fetcher = queuedFetch(pages);
  await assert.rejects(
    () => dispatch("gho_scan_ceiling", validRun(), {}, fetcher, async () => {}),
    (error) => error instanceof ServiceError && error.code === "idempotency_scan_incomplete",
  );
  assert.equal(fetcher.calls.length, 10);
  assert.match(fetcher.calls.at(-1).url, /per_page=100&page=10$/);
  assert.equal(fetcher.calls.some((call) => call.options.method !== "GET"), false);
});

test("an atomic request claim blocks a duplicate during GitHub history lag", async () => {
  const request = validRun();
  const fetcher = queuedFetch([
    { body: { workflow_runs: [] } },
    { body: { default_branch: "main" } },
    resolvedRef(),
    storedClaim(request),
  ]);
  await assert.rejects(
    () => dispatch("gho_claimed_request", request, {}, fetcher, async () => {}),
    (error) => error instanceof ServiceError && error.code === "dispatch_pending",
  );
  assert.equal(fetcher.calls.length, 4);
  assert.equal(fetcher.calls.some((call) => call.options.method === "POST"), false);
  assert.equal(fetcher.calls.some((call) => call.options.method === "PUT"), false);
});

test("a stale target ref fails before workflow or credential mutation", async () => {
  const sealed = { key_id: "key-123", encrypted_value: Buffer.alloc(64, 7).toString("base64") };
  const fetcher = queuedFetch([
    { body: { workflow_runs: [] } },
    { body: { default_branch: "main" } },
    { status: 404, body: { message: "Not Found" } },
  ]);
  await assert.rejects(() => dispatch("gho_stale_ref_token",
    validRun({ ref: "deleted/paid-branch" }),
    { OPENAI_API_KEY: sealed }, fetcher, async () => {}),
  (error) => error instanceof ServiceError && error.code === "target_ref_unresolved");
  assert.equal(fetcher.calls.length, 3);
  assert.match(fetcher.calls[2].url, /commits\/deleted%2Fpaid-branch$/);
  assert.equal(fetcher.calls.some((call) => call.options.method === "PUT"), false);
  assert.equal(fetcher.calls.some((call) => /actions\/secrets/.test(call.url)), false);
  assert.equal(fetcher.calls.some((call) => /workflows\/flexfactor-mobile/.test(call.url)), false);
});

test("sealed provider values are forwarded without accepting plaintext key fields", async () => {
  const fetcher = queuedFetch([
    { body: { workflow_runs: [] } },
    { body: { default_branch: "main" } },
    resolvedRef(),
    { status: 404, body: { message: "Not Found" } },
    missingRequestClaim(),
    createdRequestClaim(),
    installedWorkflow(),
    { status: 204 },
    acceptedDispatch(12),
    markedRequestClaim(),
  ]);
  const sealed = { key_id: "key-123", encrypted_value: Buffer.alloc(64, 7).toString("base64") };
  await dispatch("gho_sealed_token", validRun(),
    { OPENAI_API_KEY: sealed }, fetcher, async () => {});
  const secretWrite = fetcher.calls[7];
  assert.match(secretWrite.url, /actions\/secrets\/OPENAI_API_KEY$/);
  assert.equal(JSON.parse(secretWrite.options.body).encrypted_value, sealed.encrypted_value);
  assert.doesNotMatch(secretWrite.options.body, /sk-|api[_-]?key/i);
});

test("phone credentials never overwrite an owner's existing repository secret", async () => {
  const sealed = { key_id: "key-123", encrypted_value: Buffer.alloc(64, 7).toString("base64") };
  const fetcher = queuedFetch([
    { body: { workflow_runs: [] } },
    { body: { default_branch: "main" } },
    resolvedRef(),
    { body: { name: "OPENAI_API_KEY", created_at: "2026-01-01T00:00:00Z" } },
    missingRequestClaim(),
    createdRequestClaim(),
    installedWorkflow(),
    acceptedDispatch(13),
    markedRequestClaim(),
  ]);
  const result = await dispatch("gho_durable_secret", validRun(),
    { OPENAI_API_KEY: sealed }, fetcher, async () => {});
  assert.equal(result.id, 13);
  const secretCalls = fetcher.calls.filter((call) => /actions\/secrets\/OPENAI_API_KEY$/.test(call.url));
  assert.equal(secretCalls.length, 1);
  assert.equal(secretCalls[0].options.method, "GET");
  const claimCreate = JSON.parse(fetcher.calls[5].options.body);
  assert.deepEqual(JSON.parse(claimCreate.value).ephemeral_secrets, []);
});

test("a partial credential-write failure removes every phone credential and its request claim", async () => {
  const request = validRun();
  const sealed = { key_id: "key-123", encrypted_value: Buffer.alloc(64, 7).toString("base64") };
  const fetcher = queuedFetch([
    { body: { workflow_runs: [] } },
    { body: { default_branch: "main" } },
    resolvedRef(),
    { status: 404, body: { message: "Not Found" } },
    { status: 404, body: { message: "Not Found" } },
    missingRequestClaim(),
    createdRequestClaim(),
    installedWorkflow(),
    { status: 204 },
    { status: 500, body: { message: "secret unavailable" } },
    storedClaim(request, {
      ephemeral_secrets: ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
    }),
    { status: 204 },
    { status: 204 },
    { status: 204 },
  ]);
  await assert.rejects(
    () => dispatch("gho_cleanup_failure", request,
      { OPENAI_API_KEY: sealed, ANTHROPIC_API_KEY: sealed }, fetcher, async () => {}),
    (error) => error instanceof ServiceError && error.code === "github_request_failed",
  );
  assert.equal(fetcher.calls[11].options.method, "DELETE");
  assert.match(fetcher.calls[11].url, /actions\/secrets\/OPENAI_API_KEY$/);
  assert.equal(fetcher.calls[12].options.method, "DELETE");
  assert.match(fetcher.calls[12].url, /actions\/secrets\/ANTHROPIC_API_KEY$/);
  assert.equal(fetcher.calls[13].options.method, "DELETE");
  assert.match(fetcher.calls[13].url, /actions\/variables\/FLEXFACTOR_RUN_/);
});

test("every configured paid credential is accepted for the one ladder", async () => {
  const sealed = { key_id: "key-123", encrypted_value: Buffer.alloc(64, 7).toString("base64") };
  const fetcher = queuedFetch([
    { body: { workflow_runs: [] } },
    { body: { default_branch: "main" } },
    resolvedRef(),
    { status: 404, body: { message: "Not Found" } },
    { status: 404, body: { message: "Not Found" } },
    missingRequestClaim(),
    createdRequestClaim(),
    installedWorkflow(),
    { status: 204 },
    { status: 204 },
    acceptedDispatch(45),
    markedRequestClaim(),
  ]);
  const result = await dispatch("gho_ladder_token",
    validRun({ mode: "scout", scout_source: "https://source.example/features" }),
    { OPENAI_API_KEY: sealed, ANTHROPIC_API_KEY: sealed }, fetcher, async () => {});
  assert.equal(result.id, 45);
  const secretNames = new Set(fetcher.calls
    .filter((call) => /actions\/secrets\/[A-Z_]+$/.test(call.url))
    .map((call) => call.url.split("/").at(-1)));
  assert.deepEqual(secretNames, new Set(["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]));
  const dispatchCall = fetcher.calls.find((call) =>
    /flexfactor-mobile\.yml\/dispatches$/.test(call.url));
  assert.ok(dispatchCall);
  const dispatched = JSON.parse(dispatchCall.options.body);
  assert.equal(dispatched.inputs.provider, "auto");
  assert.equal(dispatched.inputs.scout_source, "https://source.example/features");
  assert.equal(Object.hasOwn(dispatched.inputs, "economy"), false);
  assert.equal(Object.hasOwn(dispatched.inputs, "use_both"), false);
});

test("the free fallback makes provider credentials optional", async () => {
  const fetcher = queuedFetch([
    { body: { workflow_runs: [] } },
    { body: { default_branch: "main" } },
    resolvedRef(),
    missingRequestClaim(),
    createdRequestClaim(),
    installedWorkflow(),
    acceptedDispatch(46),
    markedRequestClaim(),
  ]);
  const result = await dispatch("gho_free_fallback",
    validRun({ max_iterations: 6 }), {}, fetcher, async () => {});
  assert.equal(result.id, 46);
  assert.equal(fetcher.calls.some((call) => /actions\/secrets\//.test(call.url)), false);
});

test("unknown secret names fail before workflow mutation", async () => {
  const sealed = { key_id: "key-123", encrypted_value: Buffer.alloc(64, 7).toString("base64") };
  const fetcher = queuedFetch([
    { body: { workflow_runs: [] } },
    { body: { default_branch: "main" } },
    resolvedRef(),
  ]);
  await assert.rejects(() => dispatch("gho_unknown_secret",
    validRun(), { OTHER_KEY: sealed }, fetcher, async () => {}),
  (error) => error instanceof ServiceError && error.code === "invalid_secret_name");
  assert.equal(fetcher.calls.length, 3);
});
test("run status reports the active engine step", async () => {
  const request = validRun();
  const fetcher = queuedFetch([
    { body: { id: 99, status: "in_progress", conclusion: null,
      display_title: `FlexFactor audit · ${request.request_id}`,
      html_url: "https://example.invalid/run" } },
    { body: { jobs: [{ steps: [{ name: "Build", status: "completed" },
      { name: "Independent review", status: "in_progress" }] }] } },
  ]);
  const result = await runStatus("gho_status_token", "owner/project", 99,
    request.request_id, fetcher);
  assert.equal(result.step, "Independent review");
});

test("completed status deletes only this request's ephemeral secrets and claim", async () => {
  const request = validRun();
  const fetcher = queuedFetch([
    { body: { id: 99, status: "completed", conclusion: "success",
      display_title: `FlexFactor audit · ${request.request_id}`,
      html_url: "https://github.com/owner/project/actions/runs/99" } },
    storedClaim(request, {
      state: "dispatched",
      run_id: 99,
      ephemeral_secrets: ["OPENAI_API_KEY"],
    }),
    { status: 204 },
    { status: 204 },
  ]);
  const result = await runStatus("gho_cleanup_token", "owner/project", 99,
    request.request_id, fetcher);
  assert.equal(result.conclusion, "success");
  assert.match(fetcher.calls[2].url, /actions\/secrets\/OPENAI_API_KEY$/);
  assert.equal(fetcher.calls[2].options.method, "DELETE");
  assert.match(fetcher.calls[3].url, /actions\/variables\/FLEXFACTOR_RUN_/);
  assert.equal(fetcher.calls[3].options.method, "DELETE");
});

test("status refuses a mismatched run title without touching the request claim", async () => {
  const request = validRun();
  const fetcher = queuedFetch([
    { body: { id: 99, status: "completed", conclusion: "success",
      display_title: `Unrelated build · ${request.request_id}`,
      html_url: "https://github.com/owner/project/actions/runs/99" } },
  ]);
  await assert.rejects(
    () => runStatus("gho_wrong_run", "owner/project", 99, request.request_id, fetcher),
    (error) => error instanceof ServiceError && error.code === "run_identity_mismatch",
  );
  assert.equal(fetcher.calls.length, 1);
});

test("status never deletes credentials when the durable claim names another run", async () => {
  const request = validRun();
  const fetcher = queuedFetch([
    { body: { id: 99, status: "completed", conclusion: "success",
      display_title: `FlexFactor audit · ${request.request_id}`,
      html_url: "https://github.com/owner/project/actions/runs/99" } },
    storedClaim(request, {
      state: "dispatched",
      run_id: 100,
      ephemeral_secrets: ["OPENAI_API_KEY"],
    }),
  ]);
  await assert.rejects(
    () => runStatus("gho_wrong_claim", "owner/project", 99, request.request_id, fetcher),
    (error) => error instanceof ServiceError && error.code === "run_identity_mismatch",
  );
  assert.equal(fetcher.calls.length, 2);
  assert.equal(fetcher.calls.some((call) => call.options.method === "DELETE"), false);
});

test("an authoritatively missing run is terminal cleanup for phone credentials", async () => {
  const request = validRun();
  const fetcher = queuedFetch([
    { status: 404, body: { message: "Not Found" } },
    storedClaim(request, {
      state: "dispatched",
      run_id: 99,
      ephemeral_secrets: ["ANTHROPIC_API_KEY"],
    }),
    { status: 204 },
    { status: 204 },
  ]);
  await assert.rejects(
    () => runStatus("gho_missing_run", "owner/project", 99, request.request_id, fetcher),
    (error) => error instanceof ServiceError && error.status === 404,
  );
  assert.match(fetcher.calls[2].url, /actions\/secrets\/ANTHROPIC_API_KEY$/);
  assert.equal(fetcher.calls[2].options.method, "DELETE");
  assert.equal(fetcher.calls[3].options.method, "DELETE");
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
