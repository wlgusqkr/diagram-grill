# Architecture IR field guide

The machine-readable contract is `diagram.schema.json`. This guide explains
the judgment behind the fields; it does not replace schema validation.

## Root

- `schema_version` is exactly `1.0`.
- `title` is the human-facing diagram title.
- `slug` determines `diagrams/<slug>/` and uses lowercase kebab case.
- `direction` is `LR` by default or `TB` for a clear vertical pipeline.
- `approval.status` becomes `approved` only after the user approves the
  matching Architecture Brief.

## Evidence

Use structured evidence on every node and edge.

```json
{"kind": "file", "path": "infra/service.tf", "line_start": 12, "line_end": 18}
```

Paths are repository-relative. Use the narrowest useful line range. When a
fact comes from the interview, reference a declared decision instead:

```json
{"kind": "decision", "decision_id": "database-choice"}
```

## Groups and nodes

Groups are a flat list linked by optional `parent` IDs. Three levels is the
maximum. A node may set `group` to `null` when it belongs outside every drawn
boundary.

Node `status` distinguishes `observed`, `user-approved`, `inferred`, and
`unknown`. It records provenance; it is not a visual theme.

`icon` must be an exact path returned by the pinned catalog. For an unavailable
service icon, use `diagrams.generic.blank.Blank` and set `icon_fallback_for` to
the service it replaces. The compiler makes this fallback visible.

## Connections

- `sync`: a request whose caller waits for a response.
- `async`: queued or deferred work without an immediate response.
- `event`: a published notification consumed independently.
- `data`: database, object, file, or bulk data movement.

Choose the semantic type, not the color or line style. The compiler owns the
fixed visual encoding. Add a short label only when it clarifies protocol,
payload, or intent.

## Decisions and assumptions

Every `decision` stores the exact question and approved answer. Assumptions
remain `unresolved` during interview rounds, then become `user-approved` or
`rejected`. Render validation blocks unresolved assumptions.
