# Security Policy

## Supported versions

Security fixes are applied to the current `main` branch and, when releases are
published, the latest release. Older commits and superseded releases are not
maintained separately.

## Report a vulnerability privately

Do not disclose a suspected vulnerability, credential, token, private log, or
exploit detail in a public issue, pull request, discussion, or commit.

Use GitHub's private vulnerability-reporting form:

https://github.com/buckeye7066/flexfactor/security/advisories/new

Include the affected commit SHA or version, operating system, reproduction
steps, expected and observed behavior, security impact, and the smallest
redacted evidence needed to reproduce the problem. Remove credentials and
personal data before submitting.

If GitHub does not present the private reporting form, contact the repository
owner through the GitHub profile without including vulnerability details and
request a private reporting channel.

## Response targets

- Acknowledge a complete report within three business days.
- Provide an initial severity and reproducibility assessment within seven
  business days.
- Provide a status update at least every fourteen days until the report is
  resolved or closed.

These are response targets, not a guarantee that every report will be fixed on
that schedule. Coordinated disclosure timing will be agreed with the reporter
after impact and remediation are understood.

## Scope

Reports are in scope when they affect FlexFactor code, packaged artifacts,
supported launchers, containment controls, credential handling, provider
routing, the Android client, the deployed FlexFactor Cloud API, or GitHub Actions
maintained in this repository. Vulnerabilities in a
third-party service should be reported to that service unless FlexFactor's use
of it creates the vulnerability.
