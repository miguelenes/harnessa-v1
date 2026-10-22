# Security policy

## Reporting a vulnerability

Please use [GitHub's private vulnerability reporting](https://github.com/miguelenes/harnessa-v1/security/advisories/new) to report security issues. Do not disclose suspected vulnerabilities in public issues or discussions. Include affected versions or commits, impact, and steps to reproduce; avoid sharing real credentials or personal data.

The maintainers will acknowledge reports and coordinate a fix and disclosure through the private advisory. Please allow time for investigation before making a report public.

## Supported versions

Security fixes are currently made against the latest state of the `main` branch. No stable release series is published yet.

## Deployment guidance

Harnessa is under active development. Follow the [local setup guidance](https://docs.harnessa.cloud/getting-started/), keep `.env` out of version control, use unique credentials, restrict repository targets with the explicit allowlist, and expose only intended services.
