import assert from "node:assert/strict";
import { test } from "node:test";

import { ServiceError, runStatus, submitSteering } from "../lib/service.js";

const REPOSITORY = "owner/project";
const REQUEST_ID = "4d32c8e5-6f2b-4a98-a7f5-99594c49b2f8";
const CLAIM_NAME = "FLEXFACTOR_RUN_4D32C8E56F2B4A98A7F5";
const STEERING_NAME = "FLEXFACTOR_STEERING_4D32C8E56F2B4A98";
const TOKEN = "gho_steering_lifecycle_test";

// Model GitHub storage and interleave real service calls at its network boundary.
// The lifecycle itself is exercised through submitSteering and runStatus.
function githubStore() {
  const variables = new Map([[CLAIM_NAME, JSON.stringify({
    schema: 1,
    request_id: REQUEST_ID,
    state: "dispatched",
    run_id: 99,
    ephemeral_secrets: [],
    created_at: "2026-09-13T00:00:00.000Z",
  })]]);
  const state = {
    variables,
    calls: [],
    history: [],
    run: {
      event: "workflow_dispatch", path: ".github/workflows/flexfactor-mobile.yml",
      id: 99,
      status: "in_progress",
      conclusion: null,
      display_title: `FlexFactor audit · ${REQUEST_ID}`,
      html_url: "https://github.com/owner/project/actions/runs/99",
    },
    beforeSteeringWrite: null,
    afterSteeringWrite: null,
    failSteeringDeletes: 0,
    failNextClaimRead: false,
  };
  const response = (status, value) => new Response(
    status === 204 ? null : JSON.stringify(value), { status });
  state.fetch = async (url, options = {}) => {
    const path = new URL(url).pathname;
    const method = options.method || "GET";
    state.calls.push({ path, method, body: options.body });
    if (path === `/repos/${REPOSITORY}/actions/runs` && method === "GET") {
      return response(200, { workflow_runs: state.history });
    }
    if ([`/repos/${REPOSITORY}/actions/runs/99`, `/repos/${REPOSITORY}/actions/runs/100`]
      .includes(path) && method === "GET") {
      return response(200, state.run);
    }
    const prefix = `/repos/${REPOSITORY}/actions/variables`;
    if (path.startsWith(`${prefix}/`)) {
      const name = path.slice(prefix.length + 1);
      if (method === "GET") {
        if (name === CLAIM_NAME && state.failNextClaimRead) {
          state.failNextClaimRead = false;
          throw new Error("Temporary claim inspection failure");
        }
        return variables.has(name)
          ? response(200, { name, value: variables.get(name) })
          : response(404, { message: "Not Found" });
      }
      if (method === "DELETE") {
        if (name === STEERING_NAME && state.failSteeringDeletes > 0) {
          state.failSteeringDeletes -= 1;
          return response(500, { message: "Temporary deletion failure" });
        }
        const existed = variables.delete(name);
        return response(existed ? 204 : 404, { message: "Not Found" });
      }
      if (method === "PATCH") {
        if (!variables.has(name)) return response(404, { message: "Not Found" });
        const payload = JSON.parse(options.body);
        if (name === STEERING_NAME && state.beforeSteeringWrite) {
          await state.beforeSteeringWrite();
        }
        variables.set(name, payload.value);
        if (name === STEERING_NAME && state.afterSteeringWrite) {
          await state.afterSteeringWrite();
        }
        return response(204);
      }
    }
    if (path === prefix && method === "POST") {
      const payload = JSON.parse(options.body);
      if (payload.name === STEERING_NAME && state.beforeSteeringWrite) {
        await state.beforeSteeringWrite();
      }
      if (variables.has(payload.name)) return response(422, { message: "Already exists" });
      variables.set(payload.name, payload.value);
      if (payload.name === STEERING_NAME && state.afterSteeringWrite) {
        await state.afterSteeringWrite();
      }
      return response(201, {});
    }
    throw new Error(`Unexpected test request: ${method} ${path}`);
  };
  return state;
}

function isInactive(error) {
  return error instanceof ServiceError && error.code === "steering_run_inactive";
}

for (const scenario of ["missing claim", "unrecorded run", "terminal run"]) {
  test(`steering rejects ${scenario} without creating a variable`, async () => {
    const github = githubStore();
    if (scenario === "missing claim") github.variables.delete(CLAIM_NAME);
    if (scenario === "unrecorded run") {
      const claim = JSON.parse(github.variables.get(CLAIM_NAME));
      github.variables.set(CLAIM_NAME, JSON.stringify({ ...claim, state: "claimed", run_id: 0 }));
    }
    if (scenario === "terminal run") {
      github.run.status = "completed";
      github.run.conclusion = "success";
    }

    await assert.rejects(
      () => submitSteering(TOKEN, REPOSITORY, REQUEST_ID, "Run the full suite.", github.fetch),
      isInactive,
    );
    assert.equal(github.variables.has(STEERING_NAME), false);
    assert.equal(github.calls.some((call) => ["POST", "PUT", "PATCH"].includes(call.method)), false);
  });
}

for (const scenario of ["another request", "another run ID"]) {
  test(`steering rejects a run response for ${scenario}`, async () => {
    const github = githubStore();
    if (scenario === "another request") {
      github.run.display_title = "FlexFactor audit · 0d32c8e5-6f2b-4a98-a7f5-99594c49b2f8";
    } else {
      github.run.id = 100;
    }
    await assert.rejects(
      () => submitSteering(TOKEN, REPOSITORY, REQUEST_ID, "Run the full suite.", github.fetch),
      (error) => error instanceof ServiceError && error.code === "run_identity_mismatch",
    );
    assert.equal(github.calls.some((call) => call.method !== "GET"), false);
  });
}

test("active steering preserves the released request signature and stored comment format", async () => {
  const github = githubStore();
  assert.deepEqual(
    await submitSteering(TOKEN, REPOSITORY, REQUEST_ID, "Run the full suite.", github.fetch),
    { accepted: true },
  );
  const comments = JSON.parse(github.variables.get(STEERING_NAME));
  assert.equal(comments.length, 1);
  assert.equal(comments[0].comment, "Run the full suite.");
  assert.match(comments[0].id, /^[a-f0-9-]{36}$/i);
  assert.ok(Number.isFinite(Date.parse(comments[0].created_at)));
  assert.doesNotMatch(github.variables.get(STEERING_NAME), new RegExp(TOKEN));
});

test("steering reconnects to a visible active run when dispatch never recorded its run ID", async () => {
  const github = githubStore();
  const claim = JSON.parse(github.variables.get(CLAIM_NAME));
  github.variables.set(CLAIM_NAME, JSON.stringify({ ...claim, state: "claimed", run_id: 0 }));
  github.history = [github.run];

  assert.deepEqual(
    await submitSteering(TOKEN, REPOSITORY, REQUEST_ID, "Continue the review.", github.fetch),
    { accepted: true },
  );
  assert.equal(JSON.parse(github.variables.get(STEERING_NAME))[0].comment, "Continue the review.");
  assert.equal(JSON.parse(github.variables.get(CLAIM_NAME)).run_id, 0);
  assert.equal(github.calls.some((call) =>
    call.method === "PATCH" && call.path.endsWith(`/${CLAIM_NAME}`)), false);
});

test("late steering cannot recreate a variable after concurrent terminal cleanup", async () => {
  const github = githubStore();
  github.beforeSteeringWrite = async () => {
    github.run.status = "completed";
    github.run.conclusion = "success";
    const result = await runStatus(TOKEN, REPOSITORY, 99, REQUEST_ID, github.fetch);
    assert.equal(result.status, "completed");
    assert.equal(github.variables.has(CLAIM_NAME), false);
  };

  await assert.rejects(
    () => submitSteering(TOKEN, REPOSITORY, REQUEST_ID, "A late instruction.", github.fetch),
    isInactive,
  );
  assert.equal(github.variables.has(CLAIM_NAME), false);
  assert.equal(github.variables.has(STEERING_NAME), false);
  const write = github.calls.findIndex((call) => call.method === "POST");
  assert.ok(github.calls.slice(write + 1).some((call) =>
    call.method === "DELETE" && call.path.endsWith(`/${STEERING_NAME}`)));
});

test("a run completing during steering write triggers cleanup before rejecting the instruction", async () => {
  const github = githubStore();
  github.afterSteeringWrite = async () => {
    github.run.status = "completed";
    github.run.conclusion = "success";
  };

  await assert.rejects(
    () => submitSteering(TOKEN, REPOSITORY, REQUEST_ID, "A late instruction.", github.fetch),
    isInactive,
  );
  assert.equal(github.variables.has(STEERING_NAME), false);
});

test("failed postwrite steering cleanup remains visible and can be retried by terminal status", async () => {
  const github = githubStore();
  github.afterSteeringWrite = async () => {
    github.run.status = "completed";
    github.run.conclusion = "success";
    github.failSteeringDeletes = 1;
  };

  await assert.rejects(
    () => submitSteering(TOKEN, REPOSITORY, REQUEST_ID, "A late instruction.", github.fetch),
    (error) => error instanceof ServiceError && error.status >= 500,
  );
  assert.equal(github.variables.has(STEERING_NAME), true);
  assert.equal(github.variables.has(CLAIM_NAME), true);
  assert.equal(github.calls.some((call) =>
    call.method === "DELETE" && call.path.endsWith(`/${CLAIM_NAME}`)), false);

  const result = await runStatus(TOKEN, REPOSITORY, 99, REQUEST_ID, github.fetch);
  assert.equal(result.status, "completed");
  assert.equal(github.variables.has(STEERING_NAME), false);
  assert.equal(github.variables.has(CLAIM_NAME), false);
});

test("terminal status retries late steering cleanup even after the original claim was removed", async () => {
  const github = githubStore();
  github.beforeSteeringWrite = async () => {
    github.run.status = "completed";
    github.run.conclusion = "success";
    await runStatus(TOKEN, REPOSITORY, 99, REQUEST_ID, github.fetch);
    github.failSteeringDeletes = 1;
  };

  await assert.rejects(
    () => submitSteering(TOKEN, REPOSITORY, REQUEST_ID, "A late instruction.", github.fetch),
    (error) => error instanceof ServiceError && error.status >= 500,
  );
  assert.equal(github.variables.has(CLAIM_NAME), false);
  assert.equal(github.variables.has(STEERING_NAME), true);

  const result = await runStatus(TOKEN, REPOSITORY, 99, REQUEST_ID, github.fetch);
  assert.equal(result.status, "completed");
  assert.equal(github.variables.has(STEERING_NAME), false);
});

test("postwrite inspection failure does not delete steering for a potentially active run", async () => {
  const github = githubStore();
  github.afterSteeringWrite = async () => { github.failNextClaimRead = true; };

  await assert.rejects(
    () => submitSteering(TOKEN, REPOSITORY, REQUEST_ID, "Run the full suite.", github.fetch),
    (error) => error instanceof ServiceError && error.status >= 500,
  );
  assert.equal(github.variables.has(STEERING_NAME), true);
  assert.equal(github.variables.has(CLAIM_NAME), true);
  assert.equal(github.calls.some((call) => call.method === "DELETE"), false);
});

test("an ambiguous postwrite claim is preserved without deleting potentially active steering", async () => {
  const github = githubStore();
  github.afterSteeringWrite = async () => {
    const claim = JSON.parse(github.variables.get(CLAIM_NAME));
    github.variables.set(CLAIM_NAME, JSON.stringify({ ...claim, state: "claimed", run_id: 0 }));
  };

  await assert.rejects(
    () => submitSteering(TOKEN, REPOSITORY, REQUEST_ID, "Run the full suite.", github.fetch),
    isInactive,
  );
  assert.equal(github.variables.has(STEERING_NAME), true);
  assert.equal(github.variables.has(CLAIM_NAME), true);
  assert.equal(github.calls.some((call) => call.method === "DELETE"), false);
});

test("a changed postwrite run identity cannot authorize deletion of newer steering", async () => {
  const github = githubStore();
  github.afterSteeringWrite = async () => {
    const claim = JSON.parse(github.variables.get(CLAIM_NAME));
    github.variables.set(CLAIM_NAME, JSON.stringify({ ...claim, run_id: 100 }));
    github.run.id = 100;
    github.run.status = "completed";
    github.run.conclusion = "success";
  };

  await assert.rejects(
    () => submitSteering(TOKEN, REPOSITORY, REQUEST_ID, "Run the full suite.", github.fetch),
    (error) => error instanceof ServiceError && error.code === "run_identity_mismatch",
  );
  assert.equal(github.variables.has(STEERING_NAME), true);
  assert.equal(github.calls.some((call) => call.method === "DELETE"), false);
});
