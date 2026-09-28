# Security Policy

## Scope

This project scrapes public Google web pages. It does not collect credentials
or personal data, and it stores nothing beyond local caches under
`~/.cache/google-scrape-mcp/` (blocked-page samples and block statistics).

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub's
[Security Advisories](https://github.com/Maybeyes111/google-scrape-mcp/security/advisories/new)
rather than in a public issue.

Include: affected version, reproduction steps, and impact assessment. You can
expect an initial response within a few days.

## Hardening notes

- XML feeds (Google News/Trends RSS) are parsed with `defusedxml` when
  available, falling back to the stdlib parser.
- Secret scanning and push protection are enabled; CodeQL (Python) runs on
  every push and weekly.
- Dependencies are monitored by Dependabot with weekly grouped updates.
