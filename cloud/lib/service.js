import {ServiceError} from "./service-error.js";
import {
  API_VERSION,
  ENGINE_REF,
  MAX_ARTIFACT_BYTES,
  OAUTH_CLIENT_ID,
  WORKFLOW_FILE,
  WORKFLOW_PATH,
} from "./config.js";
import { mobileWorkflow } from "./workflow.js";
import {assertRequestWorkflowAbsent, createRequestWorkflow, deleteRequestWorkflow, normalizeWorkflowClaim} from "./request-workflow.js";
import { generateSteeringKeyPair, sealPrivateKeyForGitHub, steeringSecretName,
  validateMailboxClaim, mailboxIdentity, mailboxTag, draftMailboxPayload,
  assertMailbox, assertMailboxAsset, sealSteering, MAX_ENVELOPE_BYTES,
  MAX_MAILBOX_ASSETS } from "./steering-mailbox.js";

const GITHUB_API = "https://api.github.com";
const GITHUB_OAUTH = "https://github.com/login";
const REPOSITORY = /^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38})\/[A-Za-z0-9_.-]{1,100}$/;
const REF = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$/;
const FILE = /^(?!\/)(?!.*(?:^|\/)\.\.(?:\/|$))[^\r\n]{1,500}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const MODES = new Set(["refactor", "scout", "audit", "prodready"]);
const RUN_FIELDS = new Set([
  "request_id", "mode", "provider", "repository", "ref", "file", "goal",
  "guidance",
  "scout_apply", "max_cost", "threshold", "max_iterations",
]);
const ALLOWED_SECRETS = new Set(["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]);
const REPOSITORY_PAGE_SIZE = 100;
const MAX_REPOSITORY_PAGES = 100;
const MAX_IDEMPOTENCY_SCAN_PAGES = 100;
const DISPATCH_PENDING_MS = 15 * 60 * 1000;
const REQUEST_VARIABLE_PREFIX = "FLEXFACTOR_RUN_";

export {ServiceError} from "./service-error.js";

function cleanSecret(value, label, maximum = 16_384) {
  const clean = typeof value === "string" ? value.trim() : "";
  if (!clean) throw new ServiceError(401, "missing_credential", `${label} is missing.`);
  if (clean.length > maximum || /[\r\n]/.test(clean)) {
    throw new ServiceError(400, "invalid_credential", `${label} is invalid.`);
  }
  return clean;
}

function cleanPath(path) {
  if (typeof path !== "string" || !path.startsWith("/") || path.includes("://")
      || path.includes("\\") || /[\r\n]/.test(path)) {
    throw new ServiceError(500, "unsafe_upstream_path", "The upstream request path is invalid.");
  }
  return path;
}

function encode(value) {
  return encodeURIComponent(value);
}

function validRef(value) {
  return REF.test(value) && !value.includes("..") && !value.endsWith("/");
}

function parseJson(buffer, label = "upstream") {
  try {
    return buffer.length ? JSON.parse(buffer.toString("utf8")) : {};
  } catch {
    throw new ServiceError(502, "invalid_upstream_response", `The ${label} response was invalid.`);
  }
}

function safeGitHubError(result) {
  // Upstream error text can echo credentials or other request content.
  return `GitHub request failed (HTTP ${result.status}).`;
}

async function responseBytes(response, maximum) {
  const declared = Number(response.headers.get("content-length") || 0);
  if (declared > maximum) {
    throw new ServiceError(502, "upstream_response_too_large",
      "The upstream response was unexpectedly large.");
  }
  if (!response.body) return Buffer.alloc(0);
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = Buffer.from(value);
      total += chunk.length;
      if (total > maximum) {
        await reader.cancel();
        throw new ServiceError(502, "upstream_response_too_large",
          "The upstream response was unexpectedly large.");
      }
      chunks.push(chunk);
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks, total);
}

async function request(fetchImpl, url, options, maximum = 2 * 1024 * 1024) {
  let response;
  try {
    response = await fetchImpl(url, {
      ...options,
      signal: AbortSignal.timeout(45_000),
    });
  } catch (error) {
    if (error instanceof ServiceError) throw error;
    throw new ServiceError(503, "upstream_unavailable", "The upstream service is unavailable.");
  }
  return {
    status: response.status,
    headers: response.headers,
    body: await responseBytes(response, maximum),
  };
}

export function bearerToken(headers) {
  const source = headers?.authorization || headers?.Authorization || "";
  const match = /^Bearer ([^\s\r\n]{8,16384})$/.exec(source);
  if (!match) {
    throw new ServiceError(401, "authentication_required", "Sign in to FlexFactor again.");
  }
  return match[1];
}

export async function oauthDevice(fetchImpl = fetch) {
  const body = new URLSearchParams({
    client_id: OAUTH_CLIENT_ID,
    scope: "repo workflow offline_access",
  });
  const result = await request(fetchImpl, `${GITHUB_OAUTH}/device/code`, {
    method: "POST",
    redirect: "error",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/x-www-form-urlencoded",
      "User-Agent": "FlexFactor-Cloud/1.0",
    },
    body,
  }, 128 * 1024);
  if (result.status < 200 || result.status >= 300) {
    throw new ServiceError(502, "oauth_device_failed",
      `GitHub sign-in could not start (HTTP ${result.status}).`);
  }
  const value = parseJson(result.body, "GitHub sign-in");
  if (typeof value.device_code !== "string" || typeof value.user_code !== "string"
      || value.verification_uri !== "https://github.com/login/device"
      || !Number.isInteger(value.expires_in) || value.expires_in <= 0) {
    throw new ServiceError(502, "oauth_device_invalid",
      "GitHub returned an incomplete device sign-in response.");
  }
  return {
    device_code: value.device_code,
    user_code: value.user_code,
    verification_uri: value.verification_uri,
    expires_in: value.expires_in,
    interval: Math.max(5, Number(value.interval) || 5),
  };
}

async function oauthTokenRequest(parameters, fetchImpl = fetch) {
  const body = new URLSearchParams({
    client_id: OAUTH_CLIENT_ID,
    ...parameters,
  });
  const result = await request(fetchImpl, `${GITHUB_OAUTH}/oauth/access_token`, {
    method: "POST",
    redirect: "error",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/x-www-form-urlencoded",
      "User-Agent": "FlexFactor-Cloud/1.0",
    },
    body,
  }, 128 * 1024);
  if (result.status < 200 || result.status >= 300) {
    throw new ServiceError(502, "oauth_exchange_failed",
      `GitHub sign-in failed (HTTP ${result.status}).`);
  }
  return parseJson(result.body, "GitHub sign-in");
}

function normalizedOAuthToken(value) {
  if (typeof value.access_token !== "string" || !value.access_token.trim()) {
    const messages = new Map([
      ["authorization_pending", "Waiting for GitHub device authorization."],
      ["slow_down", "Wait before checking GitHub device authorization again."],
      ["expired_token", "The GitHub device code has expired. Start sign-in again."],
      ["access_denied", "GitHub authorization was declined. Start sign-in again."],
      ["bad_refresh_token", "The GitHub session could not be refreshed. Sign in again."],
      ["invalid_grant", "The GitHub session is no longer valid. Sign in again."],
      ["incorrect_device_code", "The GitHub device code is invalid. Start sign-in again."],
      ["incorrect_client_credentials", "GitHub could not identify the sign-in application."],
      ["unsupported_grant_type", "GitHub does not support this sign-in request."],
      ["device_flow_disabled", "GitHub device sign-in is unavailable for this application."],
    ]);
    const error = messages.has(value.error) ? value.error : "oauth_exchange_failed";
    const description = messages.get(error) || "GitHub did not return an access token. Sign in again.";
    throw new ServiceError(error === "authorization_pending" || error === "slow_down" ? 202 : 401,
      error, description);
  }
  return {
    access_token: value.access_token.trim(),
    refresh_token: typeof value.refresh_token === "string" ? value.refresh_token.trim() : "",
    expires_in: Number.isFinite(Number(value.expires_in)) ? Number(value.expires_in) : 0,
    refresh_token_expires_in: Number.isFinite(Number(value.refresh_token_expires_in))
      ? Number(value.refresh_token_expires_in) : 0,
    token_type: typeof value.token_type === "string" ? value.token_type : "bearer",
    scope: typeof value.scope === "string" ? value.scope : "",
  };
}

export async function oauthPoll(deviceCode, fetchImpl = fetch) {
  const code = cleanSecret(deviceCode, "Device code", 512);
  const value = await oauthTokenRequest({
    device_code: code,
    grant_type: "urn:ietf:params:oauth:grant-type:device_code",
  }, fetchImpl);
  return normalizedOAuthToken(value);
}

export async function oauthRefresh(refreshToken, fetchImpl = fetch) {
  const token = cleanSecret(refreshToken, "Refresh token");
  const value = await oauthTokenRequest({
    refresh_token: token,
    grant_type: "refresh_token",
  }, fetchImpl);
  return normalizedOAuthToken(value);
}

export async function githubRaw(token, method, path, body = undefined, fetchImpl = fetch) {
  const auth = cleanSecret(token, "GitHub session");
  const verb = String(method || "GET").toUpperCase();
  const options = {
    method: verb,
    redirect: "manual",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${auth}`,
      "User-Agent": "FlexFactor-Cloud/1.0",
      "X-GitHub-Api-Version": API_VERSION,
    },
  };
  if (body !== undefined && body !== null) {
    options.headers["Content-Type"] = "application/json; charset=utf-8";
    options.body = JSON.stringify(body);
  }
  return request(fetchImpl, GITHUB_API + cleanPath(path), options);
}

export async function githubJson(token, method, path, body = undefined, fetchImpl = fetch) {
  const result = await githubRaw(token, method, path, body, fetchImpl);
  if (result.status < 200 || result.status >= 300) {
    if (result.status === 401) {
      throw new ServiceError(401, "session_invalid", "Your GitHub session is no longer valid.");
    }
    throw new ServiceError(result.status >= 500 ? 502 : result.status,
      "github_request_failed", safeGitHubError(result));
  }
  return result.body.length ? parseJson(result.body, "GitHub") : {};
}

function containsScope(scopes, expected) {
  return scopes.split(",").some((value) => value.trim() === expected);
}

export async function configure(token, fetchImpl = fetch) {
  const account = await githubRaw(token, "GET", "/user", undefined, fetchImpl);
  if (account.status < 200 || account.status >= 300) {
    if (account.status === 401) {
      throw new ServiceError(401, "session_invalid", "Your GitHub session is no longer valid.");
    }
    throw new ServiceError(502, "github_request_failed", safeGitHubError(account));
  }
  const scopes = account.headers.get("x-oauth-scopes") || "";
  if (scopes && (!containsScope(scopes.toLowerCase(), "repo")
      || !containsScope(scopes.toLowerCase(), "workflow"))) {
    throw new ServiceError(403, "insufficient_scope",
      "FlexFactor needs GitHub repo and workflow access.");
  }
  const user = parseJson(account.body, "GitHub");
  if (typeof user.login !== "string" || !user.login.trim()) {
    throw new ServiceError(502, "account_not_identified", "GitHub did not identify this account.");
  }
  return { login: user.login.trim() };
}

export async function repositories(token, requestedPage = 1, fetchImpl = fetch) {
  const page = Number(requestedPage);
  if (!Number.isInteger(page) || page < 1 || page > MAX_REPOSITORY_PAGES) {
    throw new ServiceError(400, "invalid_page", "Repository page must be between 1 and 100.");
  }
  const rows = [];
  const result = await githubJson(token, "GET",
    `/user/repos?affiliation=owner,collaborator,organization_member&sort=updated&direction=desc&per_page=${REPOSITORY_PAGE_SIZE}&page=${page}`,
    undefined, fetchImpl);
  if (!Array.isArray(result)) {
    throw new ServiceError(502, "invalid_repository_response",
      "GitHub returned an invalid repository list.");
  }
  for (const item of result) {
    if (!item?.permissions?.admin || typeof item.full_name !== "string") continue;
    rows.push({
      full_name: item.full_name,
      default_branch: typeof item.default_branch === "string" ? item.default_branch : "main",
      private: Boolean(item.private),
    });
  }
  return {
    repositories: rows,
    page,
    // Keep this true on page 100 when GitHub returned a full page. The phone
    // then fails loudly at its matching safety limit instead of presenting a
    // silently truncated repository list as complete.
    has_more: result.length === REPOSITORY_PAGE_SIZE,
  };
}

export function validateRunRequest(source) {
  if (!source || typeof source !== "object" || Array.isArray(source)) {
    throw new ServiceError(400, "invalid_run", "The run request is missing.");
  }
  for (const name of Object.keys(source)) {
    if (!RUN_FIELDS.has(name)) {
      throw new ServiceError(400, "invalid_run",
        `The run request contains an unsupported field: ${name}.`);
    }
  }
  const request = {
    request_id: typeof source.request_id === "string" ? source.request_id.trim() : "",
    mode: typeof source.mode === "string" ? source.mode.trim() : "",
    provider: typeof source.provider === "string" ? source.provider.trim() : "",
    repository: typeof source.repository === "string" ? source.repository.trim() : "",
    ref: typeof source.ref === "string" ? source.ref.trim() : "",
    file: typeof source.file === "string" ? source.file.trim() : "",
    goal: typeof source.goal === "string" ? source.goal.trim() : "",
    guidance: typeof source.guidance === "string" ? source.guidance.trim() : "",
    scout_apply: source.scout_apply === true,
    max_cost: Number(source.max_cost),
    threshold: Number(source.threshold),
    max_iterations: Number(source.max_iterations),
  };
  if (!UUID.test(request.request_id)) throw new ServiceError(400, "invalid_run", "The run identifier is invalid.");
  if (!MODES.has(request.mode)) throw new ServiceError(400, "invalid_run", "Choose a FlexFactor mode.");
  if (request.provider !== "auto") {
    throw new ServiceError(400, "invalid_run",
      "FlexFactor has one model policy: best available, paid to free.");
  }
  if (!REPOSITORY.test(request.repository) || request.repository.endsWith(".")) {
    throw new ServiceError(400, "invalid_run", "Repository must be written as owner/name.");
  }
  if (!validRef(request.ref)) {
    throw new ServiceError(400, "invalid_run", "The repository branch is invalid.");
  }
  if (!Number.isFinite(request.max_cost) || request.max_cost < 1 || request.max_cost > 150) {
    throw new ServiceError(400, "invalid_run", "The cost cap must be between $1 and $150.");
  }
  if (!Number.isInteger(request.threshold) || request.threshold < 0 || request.threshold > 100) {
    throw new ServiceError(400, "invalid_run", "The acceptance threshold must be between 0 and 100.");
  }
  if (!Number.isInteger(request.max_iterations) || request.max_iterations < 1
      || request.max_iterations > 6) {
    throw new ServiceError(400, "invalid_run", "FlexFactor passes must be between 1 and 6.");
  }
  if (request.mode === "refactor") {
    if (!FILE.test(request.file)) {
      throw new ServiceError(400, "invalid_run", "Option 1 needs a repository-relative file path.");
    }
    if (request.goal.length < 3 || request.goal.length > 2_000) {
      throw new ServiceError(400, "invalid_run", "Option 1 needs a clear refactoring goal.");
    }
  } else if (request.file || request.goal) {
    throw new ServiceError(400, "invalid_run", "File and goal are only valid for Option 1.");
  }
  if (request.guidance.length > 4_000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(request.guidance)) {
    throw new ServiceError(400, "invalid_guidance",
      "Guiding prompts must contain at most 4,000 printable characters.");
  }
  if (request.mode !== "scout" && request.scout_apply) {
    throw new ServiceError(400, "invalid_run", "Scout apply is only valid for Option 2.");
  }
  return request;
}

function validateEncryptedSecret(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)
      || typeof value.key_id !== "string" || !/^[A-Za-z0-9_-]{1,200}$/.test(value.key_id)
      || typeof value.encrypted_value !== "string"
      || value.encrypted_value.length > 24_000
      || !/^[A-Za-z0-9+/]+={0,2}$/.test(value.encrypted_value)) {
    throw new ServiceError(400, "invalid_encrypted_secret",
      "The sealed provider credential is invalid.");
  }
  const bytes = Buffer.from(value.encrypted_value, "base64");
  if (bytes.length < 49 || bytes.length > 8_240
      || bytes.toString("base64").replace(/=+$/, "")
        !== value.encrypted_value.replace(/=+$/, "")) {
    throw new ServiceError(400, "invalid_encrypted_secret",
      "The sealed provider credential is invalid.");
  }
  return { key_id: value.key_id, encrypted_value: value.encrypted_value };
}

export async function providerPublicKey(token, repository, fetchImpl = fetch) {
  if (!REPOSITORY.test(repository || "")) {
    throw new ServiceError(400, "invalid_repository", "Repository must be written as owner/name.");
  }
  const key = await githubJson(token, "GET",
    `/repos/${repository}/actions/secrets/public-key`, undefined, fetchImpl);
  if (typeof key.key !== "string" || typeof key.key_id !== "string") {
    throw new ServiceError(502, "invalid_repository_key",
      "GitHub returned an invalid repository encryption key.");
  }
  return { key: key.key, key_id: key.key_id };
}

function requestSecretName(requestId, provider) {
  return `FLEXFACTOR_${requestId.replaceAll("-", "").toUpperCase()}_${provider}`;
}

function isRequestScopedSecretName(name, requestId) {
  return name === steeringSecretName(requestId)
    || [...ALLOWED_SECRETS].some((provider) => name === requestSecretName(requestId, provider));
}

async function putRepositorySecret(token, request, name, value, fetchImpl) {
  if (!isRequestScopedSecretName(name, request.request_id)) {
    throw new ServiceError(400, "invalid_secret_name", "The provider credential name is invalid.");
  }
  const sealed = validateEncryptedSecret(value);
  await githubJson(token, "PUT", `/repos/${request.repository}/actions/secrets/${name}`, {
    encrypted_value: sealed.encrypted_value,
    key_id: sealed.key_id,
  }, fetchImpl);
}

async function prepareProviderSecrets(provided) {
  const values = provided && typeof provided === "object" && !Array.isArray(provided)
    ? provided : {};

  for (const name of Object.keys(values)) {
    if (!ALLOWED_SECRETS.has(name)) {
      throw new ServiceError(400, "invalid_secret_name",
        "The provider credential name is invalid.");
    }
  }

  const writes = [];
  for (const name of ALLOWED_SECRETS) {
    if (Object.prototype.hasOwnProperty.call(values, name)) {
      writes.push({ name, value: validateEncryptedSecret(values[name]) });
    }
  }
  return writes;
}

function ephemeralProviderWrites(request, writes) {
  // Never share phone-secret names across requests. An older terminal cleanup
  // may still be in flight after the phone has admitted its next queued run.
  // Canonical owner-managed secrets remain untouched as the no-phone fallback.
  return writes.map((item) => ({ ...item, provider: item.name,
    name: requestSecretName(request.request_id, item.name) }));
}

async function applyProviderSecrets(token, request, writes, fetchImpl) {
  for (const item of writes) {
    await putRepositorySecret(token, request, item.name, item.value, fetchImpl);
  }
}

async function assertTargetRef(token, repository, ref, fetchImpl) {
  const response = await githubRaw(token, "GET",
    `/repos/${repository}/commits/${encode(ref)}`, undefined, fetchImpl);
  if (response.status === 200) {
    const commit = parseJson(response.body, "GitHub target ref");
    if (typeof commit?.sha === "string" && /^[0-9a-f]{40}$/i.test(commit.sha)) return;
    throw new ServiceError(502, "invalid_target_ref_response",
      "GitHub returned an invalid target ref response.");
  }
  if (response.status === 404 || response.status === 422) {
    throw new ServiceError(409, "target_ref_unresolved",
      `The selected target ref '${ref}' no longer exists. Select a current branch, tag, or commit and try again.`);
  }
  if (response.status === 401) {
    throw new ServiceError(401, "session_invalid", "Your GitHub session is no longer valid.");
  }
  throw new ServiceError(response.status >= 500 ? 502 : response.status,
    "github_request_failed", safeGitHubError(response));
}

async function installWorkflowThroughPullRequest(token, repository, baseBranch, expected, fetchImpl) {
  const metadata = await githubJson(token, "GET", `/repos/${repository}`, undefined, fetchImpl);
  const ownerLogin = typeof metadata?.owner?.login === "string" ? metadata.owner.login : "";
  const base = await githubJson(token, "GET",
    `/repos/${repository}/git/ref/heads/${encode(baseBranch)}`, undefined, fetchImpl);
  const baseSha = typeof base?.object?.sha === "string" ? base.object.sha : "";
  if (!baseSha) {
    throw new ServiceError(409, "protected_branch_unresolved",
      "The protected target branch could not be resolved.");
  }
  // Recheck the exact base commit before creating a branch: the first write
  // may have raced an engine upgrade on the protected default branch.
  const contentPath = `/repos/${repository}/contents/${WORKFLOW_PATH}`;
  const existing = await githubRaw(token, "GET",
    `${contentPath}?ref=${encode(baseSha)}`, undefined, fetchImpl);
  if (existing.status !== 200 && existing.status !== 404) {
    throw new ServiceError(existing.status >= 500 ? 502 : existing.status,
      "github_request_failed", safeGitHubError(existing));
  }
  if (existing.status === 200) {
    const current = parseJson(existing.body, "GitHub");
    if (typeof current.content === "string") {
      const actual = Buffer.from(current.content.replace(/\n/g, ""), "base64").toString("utf8");
      if (actual === expected) return false;
      assertNoEngineDowngrade(actual);
    }
  }
  const installBranch = `flexfactor/mobile-runner-${crypto.randomUUID().slice(0, 8)}`;
  await githubJson(token, "POST", `/repos/${repository}/git/refs`, {
    ref: `refs/heads/${installBranch}`,
    sha: baseSha,
  }, fetchImpl);
  const write = {
    message: "Install FlexFactor Mobile runner",
    content: Buffer.from(expected, "utf8").toString("base64"),
    branch: installBranch,
  };
  if (existing.status === 200) {
    const contentSha = parseJson(existing.body, "GitHub").sha;
    if (contentSha) write.sha = contentSha;
  }
  await githubJson(token, "PUT", contentPath, write, fetchImpl);
  const created = await githubRaw(token, "POST", `/repos/${repository}/pulls`, {
    title: "Install FlexFactor Mobile runner",
    head: installBranch,
    base: baseBranch,
    body: "Installs the pinned FlexFactor Android caller workflow on a protected branch.",
  }, fetchImpl);
  let pull;
  if (created.status >= 200 && created.status < 300) {
    pull = parseJson(created.body, "GitHub");
  } else if (created.status === 422 && ownerLogin) {
    const rows = await githubJson(token, "GET",
      `/repos/${repository}/pulls?state=open&head=${encode(`${ownerLogin}:${installBranch}`)}&base=${encode(baseBranch)}&per_page=10`,
      undefined, fetchImpl);
    if (Array.isArray(rows) && rows.length) pull = rows[0];
  } else {
    throw new ServiceError(created.status >= 500 ? 502 : created.status,
      "github_request_failed", safeGitHubError(created));
  }
  if (!pull?.number) {
    throw new ServiceError(409, "workflow_installation_pending",
      "The protected branch requires a runner installation pull request, but GitHub did not return it.");
  }
  const merged = await githubRaw(token, "PUT",
    `/repos/${repository}/pulls/${pull.number}/merge`, {
      merge_method: "squash",
      commit_title: "Install FlexFactor Mobile runner",
    }, fetchImpl);
  if (merged.status >= 200 && merged.status < 300
      && parseJson(merged.body, "GitHub").merged === true) return true;
  throw new ServiceError(409, "workflow_installation_pending",
    `This repository protects ${baseBranch}. FlexFactor opened the runner installation PR${pull.html_url ? `: ${pull.html_url}` : "."} GitHub's configured approvals must complete before its first phone run.`);
}

function assertNoEngineDowngrade(workflow) {
  // A rolled-back control plane must never replace a newer installed engine.
  // Compare numeric components, not strings (3.5.10 is newer than 3.5.5).
  const expected = ENGINE_REF.slice("android-v".length).split(".").map(BigInt);
  const pins = workflow.matchAll(/^[ \t]*uses:[ \t]*["']?buckeye7066\/flexfactor\/\.github\/workflows\/mobile-run\.yml@(android-v(\d+)\.(\d+)\.(\d+))["']?[ \t]*(?:#.*)?\r?$/gmi);
  for (const pin of pins) {
    const installed = pin.slice(2, 5).map(BigInt);
    for (let part = 0; part < 3; part += 1) {
      if (installed[part] < expected[part]) break;
      if (installed[part] > expected[part]) {
        throw new ServiceError(409, "engine_downgrade_blocked",
          `This repository already uses ${pin[1]}, newer than the cloud engine ${ENGINE_REF}. Update the FlexFactor cloud service before retrying; the installed runner was preserved.`);
      }
    }
  }
}

async function ensureTargetWorkflow(token, repository, branch, fetchImpl) {
  const path = `/repos/${repository}/contents/${WORKFLOW_PATH}?ref=${encode(branch)}`;
  const existing = await githubRaw(token, "GET", path, undefined, fetchImpl);
  const expected = mobileWorkflow();
  let sha = "";
  if (existing.status === 200) {
    const current = parseJson(existing.body, "GitHub");
    sha = typeof current.sha === "string" ? current.sha : "";
    if (typeof current.content === "string") {
      const actual = Buffer.from(current.content.replace(/\n/g, ""), "base64").toString("utf8");
      if (actual === expected) return false;
      assertNoEngineDowngrade(actual);
    }
  } else if (existing.status !== 404) {
    throw new ServiceError(existing.status >= 500 ? 502 : existing.status,
      "github_request_failed", safeGitHubError(existing));
  }
  const payload = {
    message: sha ? "Update FlexFactor Mobile runner" : "Install FlexFactor Mobile runner",
    content: Buffer.from(expected, "utf8").toString("base64"),
    branch,
  };
  if (sha) payload.sha = sha;
  const written = await githubRaw(token, "PUT",
    `/repos/${repository}/contents/${WORKFLOW_PATH}`, payload, fetchImpl);
  if (written.status >= 200 && written.status < 300) return true;
  if ([403, 409, 422].includes(written.status)) {
    return installWorkflowThroughPullRequest(token, repository, branch, expected, fetchImpl);
  }
  throw new ServiceError(written.status >= 500 ? 502 : written.status,
    "github_request_failed", safeGitHubError(written));
}

function workflowInputs(request, providerSecretWrites, steering) {
  const cost = Number.isInteger(request.max_cost) ? String(request.max_cost) : String(request.max_cost);
  return {
    request_id: request.request_id,
    mode: request.mode,
    provider: "auto",
    steering_release_id: String(steering.release_id),
    steering_secret_name: steeringSecretName(request.request_id),
    openai_secret_name: providerSecretWrites.find((item) => item.provider === "OPENAI_API_KEY")?.name || "",
    anthropic_secret_name: providerSecretWrites.find((item) => item.provider === "ANTHROPIC_API_KEY")?.name || "",
    target_ref: request.ref,
    file: request.file,
    goal: request.goal,
    guidance: request.guidance,
    scout_apply: String(request.scout_apply),
    max_cost: cost,
    threshold: String(request.threshold),
    max_iterations: String(request.max_iterations),
  };
}

function requestVariableName(requestId) {
  const compact = String(requestId || "").replaceAll("-", "");
  if (!/^[A-Fa-f0-9]{32}$/.test(compact)) {
    throw new ServiceError(400, "invalid_request_id", "Run request ID is invalid.");
  }
  return REQUEST_VARIABLE_PREFIX + compact.slice(0, 20).toUpperCase();
}

function normalizeRequestClaim(value, request, label = "GitHub request claim") {
  let claim;
  try { claim = JSON.parse(value || ""); }
  catch { claim = null; }
  const names = claim?.ephemeral_secrets;
  if (!claim || claim.schema !== 1 || claim.request_id !== request.request_id
      || !["claimed", "dispatched"].includes(claim.state)
      || !Array.isArray(names)
      || names.some((name) => !ALLOWED_SECRETS.has(name)
        && !isRequestScopedSecretName(name, request.request_id))
      || !Number.isSafeInteger(Number(claim.run_id || 0))
      || Number(claim.run_id || 0) < 0) {
    throw new ServiceError(409, "idempotency_claim_invalid",
      `${label} is invalid or belongs to another request.`);
  }
  return {
    schema: 1,
    request_id: claim.request_id,
    state: claim.state,
    run_id: Number(claim.run_id || 0),
    ephemeral_secrets: [...new Set(names)],
    created_at: typeof claim.created_at === "string" ? claim.created_at : "",
    ...(claim.steering ? { steering: validateMailboxClaim(claim.steering, request.request_id, request.repository) } : {}),
    ...(claim.workflow ? { workflow: normalizeWorkflowClaim(claim.workflow, request.request_id) } : {}),
  };
}

function requestClaimValue(request, ephemeralSecrets, state = "claimed", runId = 0,
    createdAt = new Date().toISOString()) {
  return JSON.stringify({
    schema: 1,
    request_id: request.request_id,
    state,
    run_id: runId,
    ephemeral_secrets: [...new Set(ephemeralSecrets)],
    created_at: createdAt,
  });
}

async function readRequestClaim(token, request, fetchImpl) {
  const name = requestVariableName(request.request_id);
  const path = `/repos/${request.repository}/actions/variables/${name}`;
  const existing = await githubRaw(token, "GET", path, undefined, fetchImpl);
  if (existing.status === 404) return null;
  if (existing.status < 200 || existing.status >= 300) {
    throw new ServiceError(existing.status >= 500 ? 502 : existing.status,
      "github_request_failed", safeGitHubError(existing));
  }
  const stored = parseJson(existing.body, "GitHub request claim");
  return normalizeRequestClaim(stored.value, request);
}

async function claimRequest(token, request, ephemeralSecrets, fetchImpl) {
  const prior = await readRequestClaim(token, request, fetchImpl);
  if (prior) return { owned: false, claim: prior };
  const name = requestVariableName(request.request_id);
  const claim = requestClaimValue(request, ephemeralSecrets);
  const created = await githubRaw(token, "POST",
    `/repos/${request.repository}/actions/variables`, {
      name,
      value: claim,
    }, fetchImpl);
  if (created.status >= 200 && created.status < 300) {
    return { owned: true, claim: normalizeRequestClaim(claim, request) };
  }
  if ([409, 422].includes(created.status)) {
    const raced = await readRequestClaim(token, request, fetchImpl);
    if (raced) return { owned: false, claim: raced };
  }
  throw new ServiceError(created.status >= 500 ? 502 : created.status,
    "github_request_failed", safeGitHubError(created));
}

async function markRequestDispatched(token, request, claim, runId, fetchImpl) {
  const name = requestVariableName(request.request_id);
  await githubJson(token, "PATCH",
    `/repos/${request.repository}/actions/variables/${name}`, {
      name,
      value: JSON.stringify({ ...claim, state: "dispatched", run_id: runId }),
    }, fetchImpl);
}

async function deleteEphemeralSecret(token, repository, name, fetchImpl) {
  const removed = await githubRaw(token, "DELETE",
    `/repos/${repository}/actions/secrets/${name}`, undefined, fetchImpl);
  if (removed.status !== 204 && removed.status !== 404) {
    throw new ServiceError(removed.status >= 500 ? 502 : removed.status,
      "provider_cleanup_failed", safeGitHubError(removed));
  }
}

function steeringVariableName(requestId) {
  return `FLEXFACTOR_STEERING_${requestId.replaceAll("-", "").slice(0, 16).toUpperCase()}`;
}

async function deleteSteering(token, request, fetchImpl, steering = null) {
  await deleteOwnedSteeringMailbox(token, request, steering, fetchImpl);
  const removed = await githubRaw(token, "DELETE",
    `/repos/${request.repository}/actions/variables/${steeringVariableName(request.request_id)}`,
    undefined, fetchImpl);
  if (removed.status !== 204 && removed.status !== 404) {
    throw new ServiceError(removed.status >= 500 ? 502 : removed.status,
      "steering_cleanup_failed", safeGitHubError(removed));
  }
}

async function deleteClaimResources(token, request, claim, fetchImpl) {
  // Keep the cleanup manifest until every dependent resource is gone. Each
  // delete accepts 404, so a partial failure is safe to retry on the next poll.
  await deleteRequestWorkflow((method, path, value) => githubRaw(token, method, path, value, fetchImpl), request, claim?.workflow);
  await deleteSteering(token, request, fetchImpl, claim?.steering);
  if (!claim) return false;
  for (const name of claim.ephemeral_secrets) {
    await deleteEphemeralSecret(token, request.repository, name, fetchImpl);
  }
  const variable = requestVariableName(request.request_id);
  const removed = await githubRaw(token, "DELETE",
    `/repos/${request.repository}/actions/variables/${variable}`, undefined, fetchImpl);
  if (removed.status !== 204 && removed.status !== 404) {
    throw new ServiceError(removed.status >= 500 ? 502 : removed.status,
      "idempotency_cleanup_failed", safeGitHubError(removed));
  }
  return true;
}

async function cleanupRequestClaim(token, request, run, fetchImpl) {
  // A successful, matching terminal run lookup is the proof. In particular,
  // 404 (which may also mean lost permission) cannot authorize any deletion.
  assertMatchingRun(run, request.request_id, run.id);
  if (run.status !== "completed") {
    throw new ServiceError(409, "run_not_terminal", "The run is still active; cleanup must wait.");
  }
  const claim = await readRequestClaim(token, request, fetchImpl);
  if (claim && claim.run_id > 0 && claim.run_id !== Number(run.id)) {
    throw new ServiceError(409, "run_identity_mismatch",
      "The request claim belongs to a different GitHub run; no credential was deleted.");
  }
  return deleteClaimResources(token, request, claim, fetchImpl);
}

function parseInstant(value) {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : 0;
}

function runState(run, step = "Queued") {
  return {
    id: Number(run.id),
    status: typeof run.status === "string" ? run.status : "unknown",
    conclusion: typeof run.conclusion === "string" ? run.conclusion : "",
    html_url: typeof run.html_url === "string" ? run.html_url : "",
    step,
  };
}

function runBelongsToRequest(run, requestId) {
  const title = typeof run?.display_title === "string" ? run.display_title : "";
  const suffix = ` · ${requestId}`;
  if (!title.startsWith("FlexFactor ") || !title.endsWith(suffix)) return false;
  const mode = title.slice("FlexFactor ".length, -suffix.length);
  return MODES.has(mode);
}

function runUsesTrustedWorkflow(run) {
  const path = typeof run?.path === "string" ? run.path.split("@", 1)[0] : "";
  return path === WORKFLOW_PATH || run?.workflow_id === WORKFLOW_FILE;
}

function assertMatchingRun(run, requestId, expectedId) {
  if (!Number.isSafeInteger(Number(run?.id)) || Number(run.id) <= 0
      || Number(run.id) !== Number(expectedId) || !runBelongsToRequest(run, requestId)
      || !runUsesTrustedWorkflow(run)) {
    throw new ServiceError(409, "run_identity_mismatch",
      "GitHub returned a run that does not belong to this request.");
  }
}

async function locateDispatchedRun(token, request, workflowRef, submittedAt, fetchImpl, sleepImpl) {
  const path = `/repos/${request.repository}/actions/workflows/${WORKFLOW_FILE}/runs?event=workflow_dispatch&branch=${encode(workflowRef)}&per_page=30`;
  const earliest = submittedAt - 10_000;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const page = await githubJson(token, "GET", path, undefined, fetchImpl);
    if (Array.isArray(page.workflow_runs)) {
      const found = page.workflow_runs.find((run) =>
        runBelongsToRequest(run, request.request_id)
        && parseInstant(run.created_at) >= earliest && Number(run.id) > 0);
      if (found) return runState(found);
    }
    await sleepImpl(1_000);
  }
  throw new ServiceError(504, "run_correlation_timeout",
    "GitHub accepted the run, but FlexFactor could not correlate its run ID within 30 seconds.");
}

async function existingDispatchedRun(token, request, fetchImpl, claim = null) {
  // GitHub does not expose a display-title search. Follow its authoritative
  // pagination links so a phone that was offline for days still recovers the
  // original request instead of starting a duplicate. The ceiling is a
  // fail-closed abuse/rate bound: if a repository has more history than we can
  // prove absent, the service refuses to dispatch rather than guessing.
  const createdAt = parseInstant(claim?.created_at);
  const createdFilter = createdAt > 0
    ? `created=${encode(`>=${new Date(createdAt - 10_000).toISOString()}`)}&` : "";
  let scanned = 0;
  const maximumPages = createdFilter ? 10 : MAX_IDEMPOTENCY_SCAN_PAGES;
  for (let pageNumber = 1; pageNumber <= maximumPages; pageNumber += 1) {
    // Search repository-wide history rather than the currently installed
    // workflow path or current default branch. Either can be renamed between
    // GitHub accepting the original dispatch and a phone crash/retry; the
    // UUID is the stable correlation key across both changes.
    // Unclaimed legacy requests require an unfiltered scan: GitHub silently
    // caps filtered searches at 1,000. Claimed requests can use their creation
    // time, but saturation is still uncertainty, never proof of absence.
    const path = `/repos/${request.repository}/actions/runs?${createdFilter}per_page=100&page=${pageNumber}`;
    const response = await githubRaw(token, "GET", path, undefined, fetchImpl);
    if (response.status < 200 || response.status >= 300) {
      throw new ServiceError(response.status >= 500 ? 502 : response.status,
        "github_request_failed", safeGitHubError(response));
    }
    const page = parseJson(response.body, "GitHub workflow runs");
    if (!Array.isArray(page.workflow_runs)) {
      throw new ServiceError(502, "invalid_workflow_runs",
        "GitHub returned an invalid workflow run list.");
    }
    const found = page.workflow_runs.find((item) =>
      item.event === "workflow_dispatch" && runBelongsToRequest(item, request.request_id)
      && runUsesTrustedWorkflow(item)
      && Number.isSafeInteger(Number(item.id)) && Number(item.id) > 0);
    if (found) return runState(found);
    scanned += page.workflow_runs.length;
    const link = response.headers.get("link") || "";
    if (!/(?:^|,)\s*<[^>]+>;\s*rel="next"(?:\s*,|$)/i.test(link)) {
      if ((createdFilter && scanned >= 1000) || Number(page.total_count) > scanned) break;
      return null;
    }
  }
  throw new ServiceError(503, "idempotency_scan_incomplete",
    "FlexFactor could not prove this request ID was absent from workflow history; no duplicate was dispatched.");
}

function dispatchRecoveryRequired(requestId) {
  return new ServiceError(409, "dispatch_recovery_required",
    `Request ${requestId}: dispatch acceptance is unresolved. Inspect this UUID in GitHub Actions; retry the same request to recover its run. If none appears, contact the service operator. Do not delete the claim or start a replacement while the original may still run.`);
}

async function recoverClaimedRequest(token, request, claim, fetchImpl) {
  if (claim.run_id > 0) {
    const recovered = await githubRaw(token, "GET",
      `/repos/${request.repository}/actions/runs/${claim.run_id}`, undefined, fetchImpl);
    if (recovered.status === 404) throw dispatchRecoveryRequired(request.request_id);
    if (recovered.status < 200 || recovered.status >= 300) {
      throw new ServiceError(recovered.status >= 500 ? 502 : recovered.status,
        "github_request_failed", safeGitHubError(recovered));
    }
    const run = parseJson(recovered.body, "GitHub workflow run");
    assertMatchingRun(run, request.request_id, claim.run_id);
    return runState(run);
  }
  try {
    const found = await existingDispatchedRun(token, request, fetchImpl, claim);
    // Recovery never PATCHes or DELETEs a claim read by another invocation.
    // Repository variables do not provide compare-and-swap ownership transfer.
    if (found) return found;
  } catch (error) {
    if (error instanceof ServiceError && error.code === "idempotency_scan_incomplete") {
      throw dispatchRecoveryRequired(request.request_id);
    }
    throw error;
  }
  const createdAt = parseInstant(claim.created_at);
  if (!createdAt || Date.now() - createdAt >= DISPATCH_PENDING_MS || createdAt > Date.now()) {
    throw dispatchRecoveryRequired(request.request_id);
  }
  throw new ServiceError(409, "dispatch_pending",
    "This run request is already claimed. Retry the same request to recover its GitHub run; a duplicate will not be dispatched.");
}

export async function dispatch(token, source, encryptedSecrets = {}, fetchImpl = fetch,
    sleepImpl = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))) {
  const run = validateRunRequest(source);
  // request_id is the idempotency key shared with the durable phone queue.
  // Recover an already accepted run before revalidating mutable repository
  // state: its saved target ref or caller workflow may have been deleted after
  // GitHub accepted the original dispatch but before the phone stored the ID.
  const priorClaim = await readRequestClaim(token, run, fetchImpl);
  if (priorClaim) {
    const age = Date.now() - parseInstant(priorClaim.created_at);
    // A preparation lease outlives the cloud's five-minute invocation limit.
    // Only a definitely pre-dispatch stale claim may be cleaned and retried.
    if (priorClaim.run_id === 0 && ['preparing', 'ready'].includes(priorClaim.steering?.phase)
        && age >= DISPATCH_PENDING_MS && parseInstant(priorClaim.created_at) > 0) {
      const existing = await existingDispatchedRun(token, run, fetchImpl, priorClaim);
      if (existing) return existing;
      await deleteClaimResources(token, run, priorClaim, fetchImpl);
    } else return recoverClaimedRequest(token, run, priorClaim, fetchImpl);
  }
  const existingRun = await existingDispatchedRun(token, run, fetchImpl);
  if (existingRun) return existingRun;
  const metadata = await githubJson(token, "GET", `/repos/${run.repository}`, undefined, fetchImpl);
  const workflowRef = typeof metadata?.default_branch === "string"
    ? metadata.default_branch.trim() : "";
  if (!validRef(workflowRef)) {
    throw new ServiceError(502, "invalid_default_branch",
      "GitHub returned an invalid default branch for this repository.");
  }
  // Resolve the exact checkout target before installing/updating the caller or
  // replacing any credential. A deleted or renamed saved ref must be a
  // mutation-free failure, including for paid runs.
  await assertTargetRef(token, run.repository, run.ref, fetchImpl);
  // Validate every optional sealed credential before installing a workflow or
  // replacing any repository secret. With none configured, the same ladder
  // continues through its subscription and free/local routes.
  const suppliedWrites = await prepareProviderSecrets(encryptedSecrets);
  const providerSecretWrites = ephemeralProviderWrites(run, suppliedWrites);
  const ephemeralNames = [...providerSecretWrites.map((item) => item.name), steeringSecretName(run.request_id)];
  await assertRequestWorkflowAbsent((method, path, value) => githubRaw(token, method, path, value, fetchImpl), run);
  const ownership = await claimRequest(token, run, ephemeralNames, fetchImpl);
  if (!ownership.owned) {
    return recoverClaimedRequest(token, run, ownership.claim, fetchImpl);
  }
  let dispatchAttempted = false;
  try {
    await ensureTargetWorkflow(
      token, run.repository, workflowRef, fetchImpl);
    await initializeSteeringMailbox(token, run, ownership.claim, fetchImpl);
    await applyProviderSecrets(token, run, providerSecretWrites, fetchImpl);
    const caller = await createRequestWorkflow(
      (method, path, value) => githubRaw(token, method, path, value, fetchImpl), run,
      async (workflow) => {
        ownership.claim.workflow = workflow;
        await persistRequestClaim(token, run, ownership.claim, fetchImpl);
      });
    ownership.claim.steering.phase = 'dispatching';
    await persistRequestClaim(token, run, ownership.claim, fetchImpl);
    const submittedAt = Date.now();
    const path = `/repos/${run.repository}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
    let result;
    for (let attempt = 0; attempt < 15; attempt += 1) {
      dispatchAttempted = true;
      result = await githubRaw(token, "POST", path, {
        ref: caller.ref,
        inputs: workflowInputs(run, providerSecretWrites, ownership.claim.steering),
        return_run_details: true,
      }, fetchImpl);
      if (result.status >= 200 && result.status < 300) break;
      // A server/proxy error can arrive after GitHub accepted the POST. Only
      // an explicit client rejection permits the owner to roll back its claim.
      dispatchAttempted = ![400, 401, 403, 404, 422].includes(result.status);
      if (![404, 422].includes(result.status)) {
        throw new ServiceError(result.status >= 500 ? 502 : result.status,
          "github_request_failed", safeGitHubError(result));
      }
      await sleepImpl(1_000);
    }
    if (!result || result.status < 200 || result.status >= 300) {
      throw new ServiceError(502, "workflow_dispatch_failed",
        result ? safeGitHubError(result) : "The workflow dispatch was not attempted.");
    }
    let state;
    if (result.status === 200 && result.body.length) {
      const created = parseJson(result.body, "GitHub workflow dispatch");
      const id = Number(created.workflow_run_id);
      let htmlUrl;
      try { htmlUrl = new URL(created.html_url); } catch { htmlUrl = null; }
      const expectedPath = `/${run.repository}/actions/runs/${id}`.toLowerCase();
      if (!Number.isSafeInteger(id) || id <= 0
          || !htmlUrl || htmlUrl.protocol !== "https:" || htmlUrl.hostname !== "github.com"
          || htmlUrl.pathname.toLowerCase() !== expectedPath) {
        throw new ServiceError(502, "invalid_dispatch_response",
          "GitHub returned an invalid workflow run identifier.");
      }
      state = {
        id,
        status: "queued",
        conclusion: "",
        html_url: htmlUrl.toString(),
        step: "Queued",
      };
    } else if (result.status === 204) {
      // Compatibility only. The request remains claimed throughout correlation,
      // so an eventual-consistency gap can strand safely but cannot duplicate.
      state = await locateDispatchedRun(
        token, run, caller.ref, submittedAt, fetchImpl, sleepImpl);
    } else {
      throw new ServiceError(502, "invalid_dispatch_response",
        "GitHub accepted the workflow request without a run identifier.");
    }
    await markRequestDispatched(token, run, ownership.claim, state.id, fetchImpl);
    return state;
  } catch (error) {
    // Before the dispatch request is attempted, the claim and any partially
    // written phone secrets are safe to remove. Once the request crosses the
    // network boundary, acceptance is ambiguous: retain the claim so a retry
    // can recover but can never dispatch the UUID twice.
    // This invocation exclusively owns a pre-dispatch claim. No recovery path
    // releases it, so rollback cannot race a newer claim created by a retry.
    if (!dispatchAttempted && !ownership.claim.steeringCreationAmbiguous)
      await deleteClaimResources(token, run, ownership.claim, fetchImpl);
    throw error;
  }
}

function validateRunIdentity(repository, runId) {
  if (!REPOSITORY.test(repository || "")) {
    throw new ServiceError(400, "invalid_repository", "Run repository is invalid.");
  }
  const id = Number(runId);
  if (!Number.isSafeInteger(id) || id <= 0) {
    throw new ServiceError(400, "invalid_run_id", "Run ID is invalid.");
  }
  return id;
}

export async function runStatus(token, repository, runId, requestId, fetchImpl = fetch) {
  const id = validateRunIdentity(repository, runId);
  const cleanRequestId = typeof requestId === "string" ? requestId.trim() : "";
  if (!UUID.test(cleanRequestId)) {
    throw new ServiceError(400, "invalid_request_id", "Run request ID is invalid.");
  }
  const request = { request_id: cleanRequestId, repository };
  const run = await githubJson(token, "GET", `/repos/${repository}/actions/runs/${id}`,
    undefined, fetchImpl);
  assertMatchingRun(run, request.request_id, id);
  let step = typeof run.status === "string" ? run.status : "unknown";
  if (run.status !== "completed") {
    const jobs = await githubJson(token, "GET",
      `/repos/${repository}/actions/runs/${id}/jobs?per_page=20`, undefined, fetchImpl);
    outer: for (const job of Array.isArray(jobs.jobs) ? jobs.jobs : []) {
      for (const item of Array.isArray(job.steps) ? job.steps : []) {
        if (item.status === "in_progress") {
          step = typeof item.name === "string" ? item.name : step;
          break outer;
        }
      }
    }
  }
  if (run.status === "completed") {
    await cleanupRequestClaim(token, request, run, fetchImpl);
  }
  return runState(run, step);
}

export async function runArtifact(token, repository, runId, requestId, fetchImpl = fetch) {
  const id = validateRunIdentity(repository, runId);
  let cleanRequestId = typeof requestId === "string" ? requestId.trim() : "";
  if (cleanRequestId && !UUID.test(cleanRequestId)) {
    throw new ServiceError(400, "invalid_request_id", "Run request ID is invalid.");
  }
  const run = await githubJson(token, "GET", `/repos/${repository}/actions/runs/${id}`,
    undefined, fetchImpl);
  if (!cleanRequestId) {
    const title = typeof run?.display_title === "string" ? run.display_title : "";
    cleanRequestId = title.slice(title.lastIndexOf(" · ") + 3).trim();
  }
  if (!UUID.test(cleanRequestId)) {
    throw new ServiceError(400, "invalid_request_id", "Run request ID is invalid.");
  }
  assertMatchingRun(run, cleanRequestId, id);
  const page = await githubJson(token, "GET",
    `/repos/${repository}/actions/runs/${id}/artifacts?per_page=100`, undefined, fetchImpl);
  const artifact = (Array.isArray(page.artifacts) ? page.artifacts : []).find((item) =>
    !item.expired && item.name === `mobile-phone-${cleanRequestId}`
    && Number.isSafeInteger(Number(item.id)) && Number(item.id) > 0);
  if (!artifact) {
    throw new ServiceError(404, "run_details_pending",
      "Phone-readable details are not available for this run yet.");
  }
  const redirect = await githubRaw(token, "GET",
    `/repos/${repository}/actions/artifacts/${artifact.id}/zip`, undefined, fetchImpl);
  if (![302, 307].includes(redirect.status)) {
    throw new ServiceError(502, "artifact_open_failed",
      `GitHub could not open the result artifact (HTTP ${redirect.status}).`);
  }
  const location = redirect.headers.get("location") || "";
  let target;
  try { target = new URL(location); } catch { target = null; }
  const host = target?.hostname?.toLowerCase() || "";
  if (!target || target.protocol !== "https:"
      || !(host.endsWith(".blob.core.windows.net")
        || host.endsWith(".githubusercontent.com") || host.endsWith(".github.com"))) {
    throw new ServiceError(502, "untrusted_artifact_location",
      "GitHub returned an untrusted artifact location.");
  }
  const downloaded = await request(fetchImpl, target.toString(), {
    method: "GET",
    redirect: "error",
    headers: { "User-Agent": "FlexFactor-Cloud/1.0" },
  }, MAX_ARTIFACT_BYTES);
  if (downloaded.status !== 200) {
    throw new ServiceError(502, "artifact_download_failed",
      `The result artifact returned HTTP ${downloaded.status}.`);
  }
  return downloaded.body;
}

async function inspectSteeringRun(token, request, fetchImpl) {
  const claim = await readRequestClaim(token, request, fetchImpl);
  if (!claim) return { claim, run: null };
  // A reconnect may have found the dispatched run before its original caller
  // recorded the ID. Recover from history without mutating this shared claim.
  const runId = claim.run_id > 0 ? claim.run_id
    : (await existingDispatchedRun(token, request, fetchImpl, claim))?.id;
  if (!runId) return { claim, run: null };
  const run = await githubJson(token, "GET",
    `/repos/${request.repository}/actions/runs/${runId}`, undefined, fetchImpl);
  assertMatchingRun(run, request.request_id, runId);
  return { claim, run };
}

function acceptsSteering(inspection) {
  return ["queued", "in_progress", "waiting", "pending", "requested"]
    .includes(inspection.run?.status);
}

function inactiveSteeringError() {
  return new ServiceError(409, "steering_run_inactive",
    "Steering requires a correlated active run. Refresh this run's status before trying again.");
}

export async function submitSteering(token, repository, requestId, comment, fetchImpl = fetch) {
  if (!REPOSITORY.test(repository || "")) {
    throw new ServiceError(400, "invalid_repository", "Run repository is invalid.");
  }
  const cleanRequestId = typeof requestId === "string" ? requestId.trim() : "";
  if (!UUID.test(cleanRequestId)) {
    throw new ServiceError(400, "invalid_request_id", "Run request ID is invalid.");
  }
  const value = typeof comment === "string" ? comment.trim() : "";
  if (!value || value.length > 4_000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value)) {
    throw new ServiceError(400, "invalid_steering",
      "Steering comments must contain 1 to 4,000 printable characters.");
  }
  const request = { repository, request_id: cleanRequestId };
  const before = await inspectSteeringRun(token, request, fetchImpl);
  if (!acceptsSteering(before)) throw inactiveSteeringError();
  if (!before.claim?.steering || before.claim.steering.phase !== 'dispatching') {
    throw new ServiceError(409, 'steering_upgrade_required',
      'This older run has no readable private steering channel. Start a run with the updated engine.');
  }
  await uploadSteeringMessage(token, request, before.claim.steering, value, fetchImpl);
  // A terminal status poll may delete the claim while this write is in flight.
  // Remove a late write only on proven completion or an absent claim. Ambiguous
  // claims and failed inspection remain intact for a later authenticated retry.
  const after = await inspectSteeringRun(token, request, fetchImpl);
  if (after.run && Number(after.run.id) !== Number(before.run.id)) {
    throw new ServiceError(409, "run_identity_mismatch",
      "The request claim changed GitHub runs while steering was submitted. Refresh the run status.");
  }
  if (!acceptsSteering(after)) {
    if (!after.claim || after.run?.status === "completed") {
      await deleteSteering(token, request, fetchImpl, before.claim.steering);
    }
    throw inactiveSteeringError();
  }
  return { accepted: true };
}

async function ownedSteeringMailbox(token, request, steering, fetchImpl) {
  const identity = mailboxIdentity(steering, request);
  const result = await githubRaw(token, 'GET',
    `/repos/${request.repository}/releases/${identity.release_id}`, undefined, fetchImpl);
  if (result.status === 404) return null;
  if (result.status !== 200) throw new ServiceError(502, 'steering_mailbox_unavailable', safeGitHubError(result));
  const release = parseJson(result.body, 'steering mailbox');
  try { assertMailbox(release, identity); }
  catch { throw new ServiceError(409, 'steering_mailbox_changed', 'The private steering mailbox identity changed.'); }
  return release;
}
async function deleteOwnedSteeringMailbox(token, request, steering, fetchImpl) {
  if (!steering) return;
  if (!steering.release_id) {
    // A timed-out draft creation may have succeeded. Locate only our exact
    // request, public key and authenticated creator before releasing its claim.
    for (let page = 1; page <= 10; page += 1) {
      const rows = await githubJson(token, 'GET',
        `/repos/${request.repository}/releases?per_page=100&page=${page}`, undefined, fetchImpl);
      if (!Array.isArray(rows)) throw new ServiceError(502, 'steering_cleanup_failed', 'Invalid draft listing.');
      for (const row of rows) {
        if (row.tag_name !== mailboxTag(request.request_id)) continue;
        const found = { ...steering, release_id: row.id };
        await deleteOwnedSteeringMailbox(token, request, found, fetchImpl);
      }
      if (rows.length < 100) return;
    }
    throw new ServiceError(409, 'steering_cleanup_incomplete', 'The bounded draft search could not prove cleanup.');
  }
  const release = await ownedSteeringMailbox(token, request, steering, fetchImpl);
  if (!release) return;
  const identity = mailboxIdentity(steering, request);
  // Capacity admission is advisory under concurrency. Drain bounded pages,
  // keeping the cleanup claim until the parent is proven empty. A full page
  // is work to perform, never a permanently unremovable mailbox.
  for (let page = 0; page < 10; page += 1) {
    const assets = await githubJson(token, 'GET',
      `/repos/${request.repository}/releases/${identity.release_id}/assets?per_page=100`, undefined, fetchImpl);
    if (!Array.isArray(assets) || assets.length > MAX_MAILBOX_ASSETS)
      throw new ServiceError(409, 'steering_cleanup_failed', 'Invalid mailbox asset page.');
    try { assets.forEach((asset) => assertMailboxAsset(asset, identity)); }
    catch { throw new ServiceError(409, 'steering_cleanup_failed', 'Unrecognized mailbox content was preserved.'); }
    if (!assets.length) {
      const removed = await githubRaw(token, 'DELETE',
        `/repos/${request.repository}/releases/${identity.release_id}`, undefined, fetchImpl);
      if (![204, 404].includes(removed.status))
        throw new ServiceError(502, 'steering_cleanup_failed', safeGitHubError(removed));
      return;
    }
    for (const asset of assets) {
      const removed = await githubRaw(token, 'DELETE',
        `/repos/${request.repository}/releases/assets/${asset.id}`, undefined, fetchImpl);
      if (![204, 404].includes(removed.status))
        throw new ServiceError(502, 'steering_cleanup_failed', safeGitHubError(removed));
    }
  }
  throw new ServiceError(409, 'steering_cleanup_incomplete', 'Bounded cleanup made progress; retry the terminal status.');
}
async function persistRequestClaim(token, request, claim, fetchImpl) {
  await githubJson(token, 'PATCH',
    `/repos/${request.repository}/actions/variables/${requestVariableName(request.request_id)}`, {
      name: requestVariableName(request.request_id), value: JSON.stringify(claim),
    }, fetchImpl);
}
async function initializeSteeringMailbox(token, run, claim, fetchImpl) {
  const owner = await githubJson(token, 'GET', '/user', undefined, fetchImpl);
  if (!Number.isSafeInteger(owner.id) || owner.id <= 0)
    throw new ServiceError(502, 'steering_identity_unavailable', 'GitHub did not identify the request owner.');
  const pair = await generateSteeringKeyPair();
  claim.steering = { schema: 1, phase: 'preparing', release_id: 0,
    public_key: pair.public_key, author_id: owner.id };
  await persistRequestClaim(token, run, claim, fetchImpl);
  const repositoryKey = await providerPublicKey(token, run.repository, fetchImpl);
  const sealed = await sealPrivateKeyForGitHub(pair.private_key, repositoryKey);
  pair.private_key = '';
  await putRepositorySecret(token, run, steeringSecretName(run.request_id), sealed, fetchImpl);
  claim.steeringCreationAmbiguous = true;
  const release = await githubJson(token, 'POST', `/repos/${run.repository}/releases`,
    draftMailboxPayload(run, claim.steering), fetchImpl);
  claim.steering.release_id = release.id;
  assertMailbox(release, mailboxIdentity(claim.steering, run));
  claim.steering.phase = 'ready';
  delete claim.steeringCreationAmbiguous;
  await persistRequestClaim(token, run, claim, fetchImpl);
}
async function uploadSteeringMessage(token, request, steering, comment, fetchImpl) {
  const owner = await githubJson(token, 'GET', '/user', undefined, fetchImpl);
  if (owner?.id !== steering.author_id)
    throw new ServiceError(403, 'steering_owner_mismatch', 'Only the original run owner can send steering.');
  const release = await ownedSteeringMailbox(token, request, steering, fetchImpl);
  if (!release) throw new ServiceError(409, 'steering_mailbox_missing', 'The run steering mailbox is unavailable.');
  const assets = await githubJson(token, 'GET',
    `/repos/${request.repository}/releases/${release.id}/assets?per_page=100`, undefined, fetchImpl);
  if (!Array.isArray(assets) || assets.length >= MAX_MAILBOX_ASSETS - 1)
    throw new ServiceError(409, 'steering_queue_full', 'The private steering mailbox is full.');
  const box = await sealSteering(steering.public_key, request.request_id, request.repository, comment);
  const name = `steering-${crypto.randomUUID()}.json`;
  const body = JSON.stringify(box);
  const uploaded = await requestUpload(token, request.repository, release.id, name, body, fetchImpl);
  assertMailboxAsset(uploaded, mailboxIdentity(steering, request));
  return uploaded;
}
async function requestUpload(token, repository, releaseId, name, body, fetchImpl) {
  if (!REPOSITORY.test(repository) || !Number.isSafeInteger(releaseId) || releaseId <= 0
      || !/^steering-[0-9a-f-]{36}\.json$/.test(name)
      || Buffer.byteLength(body) > MAX_ENVELOPE_BYTES)
    throw new ServiceError(400, 'invalid_steering_upload', 'The encrypted message is invalid.');
  const result = await request(fetchImpl,
    `https://uploads.github.com/repos/${repository}/releases/${releaseId}/assets?name=${encode(name)}`, {
      method: 'POST', redirect: 'error', body,
      headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json',
        'Content-Type': 'application/json', 'X-GitHub-Api-Version': API_VERSION,
        'User-Agent': 'FlexFactor-Cloud/1.0' },
    }, 64 * 1024);
  if (result.status === 404) throw inactiveSteeringError();
  if (result.status !== 201)
    throw new ServiceError(502, 'steering_upload_failed', safeGitHubError(result));
  return parseJson(result.body, 'encrypted steering asset');
}
