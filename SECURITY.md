# Security Policy

## Supported Versions

Only the latest `main` branch is supported for security fixes until a tagged release policy is introduced.

## Reporting A Vulnerability

Do not open a public issue for credentials, private data exposure, unsafe external-send behavior, or a bypass of an approval gate. Contact the repository maintainers privately through the security contact configured on GitHub.

When reporting, include:

- affected file and version/commit;
- clear reproduction steps using synthetic data;
- impact and what an attacker or accidental operator could do;
- suggested mitigation, if known.

Never include real API keys, OAuth tokens, creator personal data, private email bodies, invoices, or payment details in the report.

## Secret Handling

Use environment variables or an approved secret manager for API credentials. Keep manifests, Gmail scan state, local reports, private project configuration, and source data outside the public repository.
