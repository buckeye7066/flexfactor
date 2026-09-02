export const SERVICE_NAME = "flexfactor-cloud";
export const SERVICE_VERSION = "1.1.0";
export const API_VERSION = "2026-03-10";
export const OAUTH_CLIENT_ID = (process.env.GITHUB_OAUTH_CLIENT_ID
  || "Ov23li0JXVXULhuCRr1g").trim();
export const ENGINE_REF = "android-v3.5.0";
export const WORKFLOW_PATH = ".github/workflows/flexfactor-mobile.yml";
export const WORKFLOW_FILE = "flexfactor-mobile.yml";
export const MAX_JSON_BYTES = 128 * 1024;
export const MAX_ARTIFACT_BYTES = 2 * 1024 * 1024;

export function oauthConfigured() {
  return /^[A-Za-z0-9]{16,128}$/.test(OAUTH_CLIENT_ID);
}
