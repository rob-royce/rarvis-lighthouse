# Security policy

RARVIS-Lighthouse is a v0.1.0 Community preview. It is not a hardened security boundary: it records goals, evidence and check results, and it can launch or retire agent sessions on your computer. Read what a command does before you run it against real work.

## Reporting a vulnerability

Use GitHub private vulnerability reporting on this repository: open the Security tab and choose "Report a vulnerability". This is the only reporting route. Please do not open a public issue for a vulnerability.

Include a minimal reproduction with synthetic data. Do not include credentials, real session transcripts or real goal text.

## Handling of local data

Goal text, evidence, check results, checkpoints and logs are stored in the state directory (`~/.rarvis-lighthouse` by default) and may contain sensitive information. The core makes no network connections and does not send this data to any server. Your agent tools and the services they use have their own data handling; their terms apply.

## Supported versions

Only the latest commit on the default branch is supported while the project is a preview.
