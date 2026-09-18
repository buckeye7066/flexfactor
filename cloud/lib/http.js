import { deploymentIdentity } from "./deployment.js";
import { MAX_JSON_BYTES } from "./config.js";
import { ServiceError, bearerToken } from "./service.js";

// Each warm function instance owns this small, fixed-size budget. This is
// local load shedding, not a distributed or per-user rate limit.
const OAUTH_WINDOW_MS = 60_000;
const OAUTH_WINDOW_REQUESTS = 120;
const OAUTH_CONCURRENT_REQUESTS = 8;
let oauthWindowEndsAt = 0;
let oauthWindowRequests = 0;
let oauthInFlight = 0;

function admitOAuth(response) {
  const now = Date.now();
  if (now >= oauthWindowEndsAt) {
    oauthWindowEndsAt = now + OAUTH_WINDOW_MS;
    oauthWindowRequests = 0;
  }
  if (oauthWindowRequests >= OAUTH_WINDOW_REQUESTS || oauthInFlight >= OAUTH_CONCURRENT_REQUESTS) {
    const retry = oauthWindowRequests >= OAUTH_WINDOW_REQUESTS
      ? Math.max(1, Math.ceil((oauthWindowEndsAt - now) / 1000)) : 5;
    response.setHeader("Retry-After", String(retry));
    throw new ServiceError(429, "oauth_rate_limited", "Sign-in is busy. Wait and try again.");
  }
  oauthWindowRequests += 1;
  oauthInFlight += 1;
}

export function setSecurityHeaders(response) {
  const identity = deploymentIdentity();
  if (identity.source_revision) response.setHeader("X-FlexFactor-Cloud-Source", identity.source_revision);
  if (identity.deployment_url) response.setHeader("X-FlexFactor-Deployment", identity.deployment_url);
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'");
  response.setHeader("Referrer-Policy", "no-referrer");
  response.setHeader("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload");
  response.setHeader("X-Content-Type-Options", "nosniff");
  response.setHeader("X-Frame-Options", "DENY");
}

export function sendJson(response, status, value) {
  setSecurityHeaders(response);
  response.status(status).json(value);
}

export function sendBytes(response, status, value, contentType) {
  setSecurityHeaders(response);
  response.setHeader("Content-Type", contentType);
  response.setHeader("Content-Length", String(value.length));
  response.status(status).send(value);
}

async function readStream(request) {
  const chunks = [];
  let total = 0;
  for await (const chunk of request) {
    const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += value.length;
    if (total > MAX_JSON_BYTES) {
      throw new ServiceError(413, "request_too_large", "The request body is too large.");
    }
    chunks.push(value);
  }
  return Buffer.concat(chunks).toString("utf8");
}

export async function jsonBody(request) {
  const declared = Number(request.headers?.["content-length"] || 0);
  if (declared > MAX_JSON_BYTES) {
    throw new ServiceError(413, "request_too_large", "The request body is too large.");
  }
  if (request.body !== undefined && typeof request.body !== "string" && !Buffer.isBuffer(request.body)) {
    if (!request.body || typeof request.body !== "object" || Array.isArray(request.body)) {
      throw new ServiceError(400, "invalid_json", "The request body must be a JSON object.");
    }
    let encoded;
    try { encoded = JSON.stringify(request.body); }
    catch { throw new ServiceError(400, "invalid_json", "The request body must be a JSON object."); }
    if (typeof encoded !== "string") {
      throw new ServiceError(400, "invalid_json", "The request body must be a JSON object.");
    }
    if (Buffer.byteLength(encoded, "utf8") > MAX_JSON_BYTES) {
      throw new ServiceError(413, "request_too_large", "The request body is too large.");
    }
    return request.body;
  }
  if (Buffer.isBuffer(request.body) && request.body.length > MAX_JSON_BYTES) {
    throw new ServiceError(413, "request_too_large", "The request body is too large.");
  }
  const raw = Buffer.isBuffer(request.body) ? request.body.toString("utf8")
    : typeof request.body === "string" ? request.body : await readStream(request);
  if (Buffer.byteLength(raw, "utf8") > MAX_JSON_BYTES) {
    throw new ServiceError(413, "request_too_large", "The request body is too large.");
  }
  if (!raw.trim()) return {};
  try {
    const value = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("object required");
    return value;
  } catch {
    throw new ServiceError(400, "invalid_json", "The request body must be a JSON object.");
  }
}

function sendFailure(response, error, request, requestId) {
  const safe = error instanceof ServiceError
    ? error : new ServiceError(500, "internal_error", "FlexFactor Cloud could not complete the request.");
  if (safe.status >= 500) {
    console.error(JSON.stringify({
      event: "request_failed",
      request_id: requestId,
      method: request.method,
      status: safe.status,
      code: safe.code,
    }));
  }
  sendJson(response, safe.status, { error: safe.code, message: safe.message });
}

export function endpoint({ methods, authenticated = false, binary = false, oauth = false, status = 200 }, action) {
  const allowed = new Set(methods);
  return async function handler(request, response) {
    const requestId = crypto.randomUUID();
    response.setHeader("X-Request-Id", requestId);
    let oauthAdmitted = false;
    try {
      const forwarded = request.headers?.["x-forwarded-proto"];
      if ((forwarded !== undefined && forwarded !== "https")
          || (process.env.NODE_ENV === "production" && forwarded !== "https" && !request.socket?.encrypted)) {
        throw new ServiceError(400, "https_required", "FlexFactor Cloud requires HTTPS.");
      }
      if (!allowed.has(request.method)) {
        response.setHeader("Allow", [...allowed].join(", "));
        throw new ServiceError(405, "method_not_allowed", "This request method is not allowed.");
      }
      const token = authenticated ? bearerToken(request.headers) : "";
      if (oauth) {
        admitOAuth(response);
        oauthAdmitted = true;
      }
      const result = await action(request, token);
      if (binary) sendBytes(response, status, result, "application/zip");
      else sendJson(response, status, result);
    } catch (error) {
      sendFailure(response, error, request, requestId);
    } finally {
      if (oauthAdmitted) oauthInFlight -= 1;
    }
  };
}
