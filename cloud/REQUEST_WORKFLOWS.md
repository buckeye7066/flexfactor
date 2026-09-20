# Request-scoped mobile callers

The default-branch `flexfactor-mobile.yml` registers the dispatcher without
referencing any repository secrets. The cloud creates a caller-only Git tree
and parentless commit for each validated request. A `flexfactor-run-<UUID>` tag
selects that caller for the workflow dispatch; the target repository/ref stays
an explicit input to the unchanged pinned engine.

The generated caller names only its three exact request-scoped secrets and the
two existing canonical provider fallbacks. It does not index the secrets object
dynamically, serialize it, or inherit all secrets. Secret values remain in
GitHub's encrypted store, not in the Git tree, tag, request claim, or log.

The request claim records the tag and exact commit before publishing the tag.
An existing tag is rejected before any credential or claim write. Ambiguous
network acceptance retains the claim and tag for recovery; a definitely refused
dispatch removes them. Terminal cleanup verifies the stored commit identity and
preserves a changed or unrelated tag. Legacy claims without a caller tag retain
their existing recovery/cleanup behavior.

Do not manually retarget tags in this reserved namespace. The registration
workflow is not a substitute for application dispatch: it carries no steering
credential, so direct/manual invocation cannot perform a successful app run.
Deployment of cloud 1.1.16 activates this transport. Engine 3.5.15, model/spend
policy, signed Android binaries and phone encryption are unchanged.

Verification must distinguish repository tests from actual live workload
acceptance. Cloud tests exercise real dispatch/recovery functions with a
synthetic GitHub transport; they do not certify 25 live competitors or a
physical-phone journey.
