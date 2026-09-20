import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";
import { dispatch } from "../lib/service.js";
import { mobileWorkflow } from "../lib/workflow.js";

const repository = "owner/project";
const workflowPath = ".github/workflows/flexfactor-mobile.yml";
const baseSha = "b".repeat(40);
const oldContent = "name: old caller\n";
const request = () => ({ request_id: randomUUID(), mode: "audit", provider: "auto",
  repository, ref: "main", max_cost: 1, threshold: 90, max_iterations: 1 });
const response = (body, status = 200, headers = {}) => new Response(
  status === 204 ? null : JSON.stringify(body), { status, headers });

// Stateful GitHub fixture: calls still go through the public dispatch service.
// No native runner, real account, credential, or network is used by these tests.
class InstallationGitHub {
  branches = new Map();
  commits = new Map([[baseSha, { content: oldContent, files: [] }]]);
  pulls = [];
  merges = [];
  calls = [];
  refsCreated = 0;
  writes = 0;
  failWrite = false;
  failPull = false;
  failList = false;
  nextNumber = 400;
  fetch = async (address, options = {}) => {
    const url = new URL(address);
    const path = url.pathname.replace(`/repos/${repository}`, "");
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : null;
    this.calls.push({ path, method, body });
    if (path.startsWith("/actions/variables")) {
      return response({}, method === "GET" ? 404 : method === "DELETE" ? 204 : 201);
    }
    if (method === "DELETE" && path.startsWith("/actions/secrets/")) return response({}, 204);
    if (path === "/actions/runs") return response({ workflow_runs: [], total_count: 0 });
    if (path === "") return response({ default_branch: "main", owner: { login: "owner" } });
    if (path === "/commits/main") return response({ sha: baseSha });
    if (path.startsWith("/git/ref/heads/")) {
      const branch = decodeURIComponent(path.slice("/git/ref/heads/".length));
      const sha = branch === "main" ? baseSha : this.branches.get(branch);
      return response(sha ? { object: { sha } } : {}, sha ? 200 : 404);
    }
    if (path === "/git/refs" && method === "POST") {
      const branch = body.ref.replace("refs/heads/", "");
      if (this.branches.has(branch)) return response({ message: "Reference already exists" }, 422);
      this.branches.set(branch, body.sha);
      this.refsCreated += 1;
      return response({ object: { sha: body.sha } }, 201);
    }
    if (path.startsWith("/compare/")) {
      const sha = path.split("...")[1];
      return response({ files: this.commits.get(sha)?.files || [] });
    }
    if (path === `/contents/${workflowPath}`) {
      const ref = url.searchParams.get("ref") || body?.branch;
      const sha = ref === "main" ? baseSha : this.branches.get(ref) || ref;
      const commit = this.commits.get(sha);
      if (method === "GET") return response(commit ? {
        sha: sha === baseSha ? "old-content-sha" : `content-${sha}`,
        content: Buffer.from(commit.content).toString("base64"), encoding: "base64",
      } : {}, commit ? 200 : 404);
      if (ref === "main") return response({ message: "Protected branch" }, 403);
      if (this.failWrite) { this.failWrite = false; return response({}, 503); }
      if (!commit || body.sha !== (sha === baseSha ? "old-content-sha" : `content-${sha}`)) {
        return response({ message: "Content changed" }, 409);
      }
      const next = (++this.writes).toString(16).padStart(40, "a");
      this.commits.set(next, { content: Buffer.from(body.content, "base64").toString(),
        files: [{ filename: workflowPath, status: "modified" }] });
      this.branches.set(ref, next);
      return response({ commit: { sha: next } }, 200);
    }
    if (path === "/pulls" && method === "GET") {
      if (this.failList) return response({}, 503);
      const head = url.searchParams.get("head")?.replace("owner:", "");
      const pulls = this.pulls.filter((pull) => !head || pull.head.ref === head);
      const page = Number(url.searchParams.get("page") || 1);
      const start = (page - 1) * 100;
      const headers = pulls.length > start + 100 ? { link: '<https://api.github.com/next>; rel="next"' } : {};
      return response(pulls.slice(start, start + 100), 200, headers);
    }
    if (path === "/pulls" && method === "POST") {
      if (this.failPull) { this.failPull = false; return response({}, 503); }
      if (this.pulls.some((pull) => pull.head.ref === body.head)) return response({}, 422);
      return response(this.addPull(body.head, this.branches.get(body.head)), 201);
    }
    if (/^\/pulls\/\d+\/merge$/.test(path)) {
      this.merges.push({ number: Number(path.split("/")[2]), body });
      return response({ message: "Required approval remains pending" }, 405);
    }
    throw new Error(`Unimplemented fixture route: ${method} ${path}`);
  };
  addPull(branch, sha, extraFiles = []) {
    if (!this.commits.has(sha)) this.commits.set(sha, { content: mobileWorkflow(),
      files: [{ filename: workflowPath, status: "modified" }, ...extraFiles] });
    this.branches.set(branch, sha);
    const number = this.nextNumber++;
    const pull = { number, title: "Install FlexFactor Mobile runner", state: "open",
      head: { ref: branch, sha, repo: { full_name: repository } },
      base: { ref: "main", sha: baseSha, repo: { full_name: repository } },
      html_url: `https://github.com/${repository}/pull/${number}` };
    this.pulls.push(pull);
    return pull;
  }
}
const attempt = (github) => dispatch("fixture-owner-token", request(), {}, github.fetch, async () => {});
const pending = (github) => assert.rejects(attempt(github), (error) =>
  error.code === "workflow_installation_pending");

test("reuses a verified legacy installation PR instead of opening another", async () => {
  const github = new InstallationGitHub();
  const original = github.addPull("flexfactor/mobile-runner-legacy", "c".repeat(40));
  await pending(github);
  assert.equal(github.refsCreated, 0);
  assert.equal(github.pulls.length, 1);
  assert.deepEqual(github.merges.map((item) => item.number), [original.number]);
  assert.equal(github.merges[0].body.sha, original.head.sha);
});
test("separate installation requests share one branch and pending PR", async () => {
  const github = new InstallationGitHub();
  await pending(github);
  await pending(github);
  assert.equal(github.refsCreated, 1);
  assert.equal(github.writes, 1);
  assert.equal(github.pulls.length, 1);
});
test("concurrent requests cannot create duplicate installation branches or PRs", async () => {
  const github = new InstallationGitHub();
  await Promise.all([pending(github), pending(github)]);
  assert.equal(github.refsCreated, 1);
  assert.equal(github.writes, 1);
  assert.equal(github.pulls.length, 1);
});

for (const interruption of ["failWrite", "failPull"]) {
  test(`recovers the original installation branch after ${interruption}`, async () => {
    const github = new InstallationGitHub();
    github[interruption] = true;
    await assert.rejects(attempt(github), (error) => error.code === "github_request_failed");
    await pending(github);
    assert.equal(github.refsCreated, 1);
    assert.equal(github.writes, 1);
    assert.equal(github.pulls.length, 1);
  });
}
test("failed pending-PR discovery does not create more work", async () => {
  const github = new InstallationGitHub();
  github.failList = true;
  await assert.rejects(attempt(github), (error) => error.code === "github_request_failed");
  assert.equal(github.refsCreated, 0);
  assert.equal(github.pulls.length, 0);
});
test("looks beyond the first page before creating an installation", async () => {
  const github = new InstallationGitHub();
  for (let index = 0; index < 100; index += 1) {
    github.pulls.push({ number: index + 1, title: "Unrelated work", head: { ref: "feature" } });
  }
  const original = github.addPull("flexfactor/mobile-runner-older", "c".repeat(40));
  await pending(github);
  assert.equal(github.refsCreated, 0);
  assert.equal(github.merges[0].number, original.number);
});

for (const mismatch of ["extra file", "fork", "different workflow"]) {
  test(`never adopts an installation lookalike with ${mismatch}`, async () => {
    const github = new InstallationGitHub();
    const other = github.addPull("flexfactor/mobile-runner-untrusted", "d".repeat(40),
      mismatch === "extra file" ? [{ filename: "unrelated.js", status: "modified" }] : []);
    if (mismatch === "fork") other.head.repo.full_name = "someone/project";
    if (mismatch === "different workflow") github.commits.get(other.head.sha).content += "# changed\n";
    await pending(github);
    assert.equal(github.refsCreated, 1);
    assert.equal(github.merges.some((item) => item.number === other.number), false);
    assert.equal(github.merges[0].body.sha, github.pulls.at(-1).head.sha);
  });
}
test("does not overwrite an interrupted branch containing unrelated changes", async () => {
  const github = new InstallationGitHub();
  github.failPull = true;
  await assert.rejects(attempt(github), (error) => error.code === "github_request_failed");
  const [branch, sha] = [...github.branches][0];
  github.commits.get(sha).files.push({ filename: "private-work.js", status: "added" });
  await assert.rejects(attempt(github), (error) => error.code === "workflow_installation_pending");
  assert.equal(github.refsCreated, 1);
  assert.equal(github.branches.get(branch), sha);
  assert.equal(github.writes, 1);
  assert.equal(github.merges.length, 0);
});
