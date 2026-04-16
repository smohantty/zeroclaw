# ZeroClaw i18n Coverage and Structure

This document defines the localization structure for ZeroClaw docs and tracks current coverage.

Last refreshed: **April 16, 2026**.

## Policy

ZeroClaw maintains English as the source-of-truth documentation language and Korean as the only localized documentation tree.

## Canonical Locations

| Locale | Root entry | Docs entry | Notes |
|---|---|---|---|
| `en` | `README.md` | `docs/README.md` | Source of truth |
| `ko` | `docs/i18n/ko/README.md` | `docs/i18n/ko/SUMMARY.md` | Maintained localized docs |

## Maintenance Rules

- English docs are updated first for runtime behavior changes.
- Korean navigation should stay in sync with English entry points.
- Do not add new locale trees without restoring the docs ownership and parity process for that locale.
