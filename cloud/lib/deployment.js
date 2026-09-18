// Public build provenance only. Missing identity stays unknown, never guessed.
export function deploymentIdentity(environment = process.env) {
  const source = environment.FLEXFACTOR_CLOUD_SOURCE_SHA || "";
  const deployment = environment.VERCEL_URL || "";
  return {
    source_revision: /^[0-9a-f]{40}$/.test(source) ? source : null,
    deployment_url: /^[A-Za-z0-9-]+\.vercel\.app$/.test(deployment) ? `https://${deployment}` : null,
  };
}
