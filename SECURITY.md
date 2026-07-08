# Security Policy

## Supported Versions

CaptchAI is currently maintained from the `main` branch. Security fixes will be applied there first.

## Reporting a Vulnerability

Please do **not** open public GitHub issues for sensitive security reports.

Instead:

1. Prepare a minimal reproduction or impact description.
2. Include the affected version, deployment mode, and whether the issue requires authentication.
3. Send the report privately through GitHub Security Advisories if available for the repository, or contact the maintainer through a private channel.

## What to include

Please include as much of the following as possible:

- affected endpoint or component
- reproduction steps
- expected vs actual behavior
- logs or screenshots with secrets removed
- whether the issue is exploitable remotely or only in a local/self-hosted setup

## Secret handling

This repository is designed for public use. Do not include any of the following in issues, pull requests, screenshots, or sample files:

- API keys
- access tokens
- cookies
- private model endpoints
- customer URLs
- personally identifying data

## Operational guidance

If you deploy CaptchAI publicly:

- store secrets in environment variables or your hosting platform's secret manager
- avoid committing `.env` files
- rotate keys if they were ever exposed in logs or history
- consider placing the service behind your own authentication, rate limiting, and monitoring layers
