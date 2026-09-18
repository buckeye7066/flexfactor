import { deploymentIdentity } from "../lib/deployment.js";
import { ENGINE_REF, SERVICE_NAME, SERVICE_VERSION, oauthConfigured } from "../lib/config.js";
import { endpoint } from "../lib/http.js";

export default endpoint({ methods: ["GET"], status: oauthConfigured() ? 200 : 503 }, async () => {
  const ready = oauthConfigured();
  return {
    ...deploymentIdentity(),
    ok: ready,
    service: SERVICE_NAME,
    version: SERVICE_VERSION,
    engine_ref: ENGINE_REF,
    oauth_device_configured: ready,
  };
});
