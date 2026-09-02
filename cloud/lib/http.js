import { MAX_JSON_BYTES } from "./config.js";
import { ServiceError, bearerToken } from "./service.js";

export function setSecurityHeaders(response) {
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'");
  response.setHeader("Referrer-Policy", "no-referrer");
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
  if (request.body && typeof request.body === "object" && !Buffer.isBuffer(request.body)) {
    let encoded;
    try { encoded = JSON.stringify(request.body); }
    catch { throw new ServiceError(400, "invalid_json", "The request body must be a JSON object."); }
    if (Buffer.byteLength(encoded, "utf8") > MAX_JSON_BYTES) {
      throw new ServiceError(413, "request_too_large", "The request body is too large.");
    }
    return request.body;
  }
  const raw = typeof request.body === "string" ? request.body : await readStream(request);
  if (!raw.trim()) return {};
  if (Buffer.byteLength(raw, "utf8") > MAX_JSON_BYTES) {
    throw new ServiceError(413, "request_too_large", "The request body is too large.");
  }
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
      path: String(request.url || "").split("?", 1)[0].slice(0, 160),
      status: safe.status,
      code: safe.code,
    }));
  }
  sendJson(response, safe.status, { error: safe.code, message: safe.message });
}

export function endpoint({ methods, authenticated = false, binary = false }, action) {
  const allowed = new Set(methods);
  return async function handler(request, response) {
    const requestId = crypto.randomUUID();
    response.setHeader("X-Request-Id", requestId);
    try {
      const forwarded = request.headers?.["x-forwarded-proto"];
      if (forwarded && forwarded !== "https") {
        throw new ServiceError(400, "https_required", "FlexFactor Cloud requires HTTPS.");
      }
      if (!allowed.has(request.method)) {
        response.setHeader("Allow", [...allowed].join(", "));
        throw new ServiceError(405, "method_not_allowed", "This request method is not allowed.");
      }
      const token = authenticated ? bearerToken(request.headers) : "";
      const result = await action(request, token);
      if (binary) sendBytes(response, 200, result, "application/zip");
      else sendJson(response, 200, result);
    } catch (error) {
      sendFailure(response, error, request, requestId);
    }
  };
}
