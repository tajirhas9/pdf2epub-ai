# Security Policy

## Supported versions

Security fixes go into the latest release and the `main` branch.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately instead, either through GitHub
([Security → Report a vulnerability](https://github.com/tajirhas9/pdf2epub-ai/security/advisories/new))
or by email to **tajircuet@gmail.com**. Include:

- what is affected (CLI, web app, Docker image) and the version (`pdf2epub --version`)
- steps to reproduce, or a proof of concept
- the impact you expect (for example: reading files outside the work folder, leaking an API key,
  bypassing the web app's access key)

You should get a reply within a week. Once a fix is ready, it is released and the report is credited
(unless you prefer to stay anonymous).

## Scope notes

- The web app (`pdf2epub ui`) is meant for your own machine. Anyone with its link (which contains the
  access key) can use it; exposing it on a network with `--host 0.0.0.0` is at your own risk.
- PDFs are untrusted input. Crashes are bugs; anything that lets a crafted PDF run code or touch files
  outside the work folder is a security issue.
- API keys must never appear in logs, work files or error messages; a leak is a security issue.
