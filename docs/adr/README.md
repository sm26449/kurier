# Architecture Decision Records

Short, numbered documents that capture one decision each: the context,
the options considered, what we chose and why, and what it costs.
They are not rewritten after acceptance: a small correction is recorded
as a dated `Amended:` line under the header, and a later decision that
changes course gets a new ADR that supersedes the old one.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-watchdogs.md) | Watchdogs: an SMS when a service stops checking in | Accepted |

## Format

```
# ADR-NNNN — Title
Status: Proposed | Accepted | Superseded by ADR-MMMM
Date: YYYY-MM-DD
Amended: YYYY-MM-DD — what changed (optional, one line per amendment)

## Context        — the forces at play, in a few paragraphs
## Decision       — what we decided, stated plainly
## Consequences   — what gets easier, what gets harder, what we must do next
## Alternatives   — what else we considered and why not
```

Keep an ADR under two screens. Link to the code or docs that implement
it instead of repeating them.
