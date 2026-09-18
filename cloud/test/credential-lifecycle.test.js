import { steeringSecretName } from "../lib/steering-mailbox.js";
import { withMailboxGithub } from "../test_support/mailbox-github.js";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { dispatch, runStatus, ServiceError } from "../lib/service.js";
import { mobileWorkflow } from "../lib/workflow.js";

const firstId = "4d32c8e5-6f2b-4a98-a7f5-99594c49b2f8";
const nextId = "5d32c8e5-6f2b-4a98-a7f5-99594c49b2f8";
const repository = "owner/project";
const sealed = { key_id: "test-key", encrypted_value: Buffer.alloc(64, 7).toString("base64") };
const claimName = (id) => `FLEXFACTOR_RUN_${id.replaceAll("-", "").slice(0, 20).toUpperCase()}`;
const scopedName = (id, provider = "OPENAI_API_KEY") =>
  `FLEXFACTOR_${id.replaceAll("-", "").toUpperCase()}_${provider}`;
const request = (id) => ({ request_id: id, repository, ref: "main", mode: "audit",
  provider: "auto", max_cost: 50, threshold: 90, max_iterations: 6 });
const run = (id, requestId) => ({ id, event: "workflow_dispatch", path: ".github/workflows/flexfactor-mobile.yml", status: "completed", conclusion: "success",
  display_title: `FlexFactor audit · ${requestId}`,
  html_url: `https://github.com/${repository}/actions/runs/${id}` });
const claim = (id, names) => ({ schema: 1, request_id: id, state: "dispatched", run_id: 99,
  ephemeral_secrets: names, created_at: "2026-09-13T00:00:00.000Z" });
const response = (body, status = 200) => new Response(
  status === 204 ? null : JSON.stringify(body), { status });

function githubStore({ initialClaim, initialSecrets = [] } = {}) {
  const variables = new Map();
  if (initialClaim) variables.set(claimName(initialClaim.request_id), JSON.stringify(initialClaim));
  const secrets = new Map(initialSecrets.map((name) => [name, "existing credential"]));
  const runs = [run(99, firstId)];
  const api = { variables, secrets, runs, calls: [], hook: null };
  api.fetch = async (url, options = {}) => {
    const path = new URL(url).pathname.replace(`/repos/${repository}`, "");
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    const call = { path, method, body };
    api.calls.push(call);
    const overridden = await api.hook?.(call);
    if (overridden) return overridden;
    if (path === "/actions/variables" && method === "POST") {
      if (variables.has(body.name)) return response({}, 422);
      variables.set(body.name, body.value);
      return response({}, 201);
    }
    if (path.startsWith("/actions/variables/")) {
      const name = path.split("/").at(-1);
      if (method === "GET") return variables.has(name)
        ? response({ name, value: variables.get(name) }) : response({}, 404);
      if (method === "DELETE") { variables.delete(name); return response(null, 204); }
      if (method === "PATCH") {
        if (!variables.has(name)) return response({}, 404);
        variables.set(name, body.value);
        return response(null, 204);
      }
    }
    if (path.startsWith("/actions/secrets/")) {
      const name = path.split("/").at(-1);
      if (method === "GET") return response({}, secrets.has(name) ? 200 : 404);
      if (method === "PUT") { secrets.set(name, body.encrypted_value); return response(null, 204); }
      if (method === "DELETE") { secrets.delete(name); return response(null, 204); }
    }
    if (path === "/actions/runs") return response({ workflow_runs: runs });
    if (/^\/actions\/runs\/\d+$/.test(path)) {
      const found = runs.find((item) => item.id === Number(path.split("/").at(-1)));
      return response(found || {}, found ? 200 : 404);
    }
    if (path === "") return response({ default_branch: "main" });
    if (path === "/commits/main") return response({ sha: "a".repeat(40) });
    if (path === "/contents/.github/workflows/flexfactor-mobile.yml") {
      return response({ sha: "workflow-sha", content: Buffer.from(mobileWorkflow()).toString("base64") });
    }
    if (path === "/actions/workflows/flexfactor-mobile.yml/dispatches") {
      const created = { ...run(100, body.inputs.request_id), status: "queued", conclusion: null };
      runs.push(created);
      return response({ workflow_run_id: 100, html_url: created.html_url });
    }
    throw new Error(`Unexpected GitHub transport: ${method} ${path}`);
  };
  api.fetch = withMailboxGithub(api.fetch, { variables });
  api.mailbox = api.fetch.mailbox;
  return api;
}

for (const olderName of ["OPENAI_API_KEY", scopedName(firstId)]) {
test(`delayed cleanup of ${olderName} cannot delete the next dispatched run's phone credential`, async () => {
  const api = githubStore({ initialClaim: claim(firstId, [olderName]),
    initialSecrets: [olderName] });
  let releaseReads, releaseDelete, reachedDelete;
  const bothRead = new Promise((resolve) => { releaseReads = resolve; });
  const delayedDelete = new Promise((resolve) => { releaseDelete = resolve; });
  const atDelete = new Promise((resolve) => { reachedDelete = resolve; });
  let reads = 0, deletes = 0;
  api.hook = async ({ path, method }) => {
    if (path === `/actions/variables/${claimName(firstId)}` && method === "GET" && reads < 2) {
      const captured = api.variables.get(claimName(firstId));
      reads += 1;
      if (reads === 2) releaseReads();
      await bothRead;
      return response({ value: captured });
    }
    if (path === `/actions/secrets/${olderName}` && method === "DELETE") {
      deletes += 1;
      if (deletes === 2) { reachedDelete(); await delayedDelete; }
    }
  };
  const cleaners = [
    runStatus("test_token", repository, 99, firstId, api.fetch),
    runStatus("test_token", repository, 99, firstId, api.fetch),
  ];
  await atDelete;
  await Promise.race(cleaners);
  let liveName;
  try {
    await dispatch("test_token", request(nextId), { OPENAI_API_KEY: sealed }, api.fetch);
    const nextClaim = JSON.parse(api.variables.get(claimName(nextId)));
    [liveName] = nextClaim.ephemeral_secrets;
    assert.equal(api.secrets.has(liveName), true);
  } finally {
    releaseDelete();
    await Promise.all(cleaners);
  }
  assert.equal(api.secrets.has(liveName), true, "stale cleanup deleted the newer run's live credential");
});
}

test("phone credentials use full-request scoped secret names and caller-only dispatch inputs", async () => {
  const api = githubStore();
  await dispatch("test_token", request(nextId),
    { OPENAI_API_KEY: sealed, ANTHROPIC_API_KEY: sealed }, api.fetch);
  const names = [scopedName(nextId), scopedName(nextId, "ANTHROPIC_API_KEY")];
  assert.deepEqual([...api.secrets.keys()], names);
  assert.deepEqual(JSON.parse(api.variables.get(claimName(nextId))).ephemeral_secrets, [...names, steeringSecretName(nextId)]);
  const inputs = api.calls.find((call) => call.path.endsWith("/dispatches")).body.inputs;
  assert.equal(inputs.openai_secret_name, names[0]);
  assert.equal(inputs.anthropic_secret_name, names[1]);
  assert.equal(JSON.stringify(inputs).includes(sealed.encrypted_value), false);
});

test("an owner's canonical provider secret stays untouched beside the isolated phone credential", async () => {
  const api = githubStore({ initialSecrets: ["OPENAI_API_KEY"] });
  await dispatch("test_token", request(nextId), { OPENAI_API_KEY: sealed }, api.fetch);
  assert.equal(api.secrets.get("OPENAI_API_KEY"), "existing credential");
  assert.deepEqual(JSON.parse(api.variables.get(claimName(nextId))).ephemeral_secrets, [scopedName(nextId), steeringSecretName(nextId)]);
  assert.equal(api.secrets.get(scopedName(nextId)), sealed.encrypted_value);
  assert.equal(api.calls.some((call) => call.path === "/actions/secrets/OPENAI_API_KEY"
    && call.method !== "GET"), false);
});

test("a legacy canonical credential still present during dispatch cannot be selected for the next phone key", async () => {
  const api = githubStore({ initialClaim: claim(firstId, ["OPENAI_API_KEY"]),
    initialSecrets: ["OPENAI_API_KEY"] });
  await dispatch("test_token", request(nextId), { OPENAI_API_KEY: sealed }, api.fetch);
  await runStatus("test_token", repository, 99, firstId, api.fetch);
  assert.equal(api.secrets.has("OPENAI_API_KEY"), false);
  assert.equal(api.secrets.get(scopedName(nextId)), sealed.encrypted_value);
});

test("omitted phone credentials preserve the caller's canonical fallback", async () => {
  const api = githubStore({ initialSecrets: ["OPENAI_API_KEY"] });
  await dispatch("test_token", request(nextId), {}, api.fetch);
  const inputs = api.calls.find((call) => call.path.endsWith("/dispatches")).body.inputs;
  assert.equal(inputs.openai_secret_name, "");
  assert.equal(inputs.anthropic_secret_name, "");
  assert.equal(api.secrets.get("OPENAI_API_KEY"), "existing credential");
  assert.deepEqual(JSON.parse(api.variables.get(claimName(nextId))).ephemeral_secrets, [steeringSecretName(nextId)]);
});

test("generated caller maps scoped inputs to the pinned engine's existing secret interface", () => {
  const workflow = mobileWorkflow();
  const engine = readFileSync(new URL("../../.github/workflows/mobile-run.yml", import.meta.url), "utf8");
  const declarations = engine.split("  workflow_call:")[1].split("permissions:")[0]
    .split("    secrets:")[1];
  const names = [...declarations.matchAll(/^      ([A-Z_]+):/gm)].map((item) => item[1]);
  assert.deepEqual(names, ["STEERING_PRIVATE_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]);
  for (const [input, name] of [["openai_secret_name", "OPENAI_API_KEY"], ["anthropic_secret_name", "ANTHROPIC_API_KEY"]]) {
    assert.match(workflow, new RegExp(`      ${input}:\\n        required: false\\n        type: string`));
    assert.ok(workflow.includes(name + ": " + "${{ secrets[inputs." + input + "] || secrets." + name + " }}"));
    assert.equal(workflow.split("    with:")[1].split("    secrets:")[0].includes(`${input}:`), false);
  }
  assert.equal(workflow.includes("secrets: inherit"), false);
});

test("terminal cleanup accepts exact request-scoped manifest credentials", async () => {
  const name = scopedName(firstId);
  const api = githubStore({ initialClaim: claim(firstId, [name]), initialSecrets: [name] });
  await runStatus("test_token", repository, 99, firstId, api.fetch);
  assert.equal(api.secrets.has(name), false);
});

test("a claim naming another full request UUID cannot authorize credential deletion", async () => {
  const otherId = "4d32c8e5-6f2b-4a98-a7f5-99594c49b2f9";
  const name = scopedName(otherId);
  const api = githubStore({ initialClaim: claim(firstId, [name]), initialSecrets: [name] });
  await assert.rejects(() => runStatus("test_token", repository, 99, firstId, api.fetch),
    (error) => error instanceof ServiceError && error.code === "idempotency_claim_invalid");
  assert.equal(api.secrets.has(name), true);
  assert.equal(api.calls.some((call) => call.method === "DELETE"), false);
});
