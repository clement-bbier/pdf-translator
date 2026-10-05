---
name: decision-record
description: How to write a short architecture decision note in docs/decisions/ (numbering, template, tone). Use when a change introduces a dependency, a data format, a trade-off or any choice a contributor would later ask "why?" about.
---

# Writing a decision note

One note per significant choice, in `docs/decisions/NNNN-short-title.md`.
`NNNN` is the next free four-digit number (`ls docs/decisions`). Notes are
never renumbered or deleted: a reversed decision gets a new note whose status
says which one it supersedes, and the old note's status becomes
`Superseded by NNNN`.

Write a note when the choice is hard to reverse or surprising: a new
dependency, a storage or file format, an algorithm picked over an obvious
alternative, a limitation accepted on purpose. Do not write one for routine
changes.

## Template

```markdown
# NNNN. <Decision as a short statement>

- Status: Accepted
- Date: YYYY-MM-DD

## Context

What problem forced a choice. Facts and constraints only: requirements,
measurements, limits of the current code.

## Decision

What we do, stated plainly, with the key parameters.

## Alternatives considered

- <Alternative>: why it was not chosen (one or two lines each).

## Consequences

What becomes easier, what becomes harder, what we must watch. Include the
known limitations.
```

## Tone

Factual and short (roughly 200-400 words). Written for a contributor who
was not there. No marketing language, no superlatives. Cite numbers when
they drove the decision. English.
