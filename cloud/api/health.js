import { ENGINE_REF, SERVICE_NAME, SERVICE_VERSION, oauthConfigured } from "../lib/config.js";
import { sendJson, setSecurityHeaders } from "../lib/http.js";

export default async function handler(request, response) {
  setSecurityHeaders(response);
  if (request.method !== "GET") {
    response.setHeader("Allow", "GET");
    return sendJson(response, 405, { error: "method_not_allowed", message: "This request method is not allowed." });
  }
  const ready = oauthConfigured();
  return sendJson(response, ready ? 200 : 503, {
    ok: ready,
    service: SERVICE_NAME,
    version: SERVICE_VERSION,
    engine_ref: ENGINE_REF,
    oauth_device_configured: ready,
  });
}
