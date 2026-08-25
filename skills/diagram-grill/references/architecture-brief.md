# Architecture Brief Contract

Present this brief in the conversation before creating any diagram artifact.
It is the human review gate between repository inspection and the constrained
diagram IR. Do not save a draft brief, JSON specification, generated Python, or
rendered image to the repository before approval.

Use concise tables. Replace every placeholder, use repository-relative evidence
paths, and write `None` when a section has no entries. Keep observed facts,
user decisions, assumptions, and unknowns visibly distinct.

## Architecture Brief template

```markdown
# Architecture Brief: <title>

- Revision: <stable revision identifier>
- Diagram question: <the one question this view answers>
- Audience: <intended readers>
- Status: Awaiting approval

## Scope

- In scope: <systems, environments, and concerns shown>
- Out of scope: <nearby concerns intentionally omitted>
- System boundary: <what is owned versus external>
- Abstraction level: <context, logical component, deployment, network, or data flow>

## Direction and view plan

- Direction: <LR or TB>
- Direction source: <user override or agent-selected rule>
- Reason: <user request, clear pipeline, or LR default>
- Views: <single view, or overview plus named detail views>
- Split reason: <more than 12 nodes, mixed abstraction levels, or None>

## Groups

| ID | Label | Parent | Boundary meaning | Evidence or decision |
| --- | --- | --- | --- | --- |
| <group-id> | <visible label> | <parent-id or None> | <environment, region, network, namespace, trust boundary, etc.> | <evidence-ref or decision-id> |

Maximum group depth: <1, 2, or 3>

## Nodes

| ID | Label | Responsibility | Exact icon import | Group | Status | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| <node-id> | <visible label> | <one responsibility> | diagrams.<provider>.<category>.<Class> | <group-id or None> | observed / user-approved / inferred / unknown | <evidence-ref or decision-id> |

Generic fallbacks:

| Node ID | Required icon | Visible replacement | Reason |
| --- | --- | --- | --- |
| <node-id> | <requested product or concept> | diagrams.generic.blank.Blank | <why no exact catalog entry exists> |

## Typed connections

| ID | From | To | Type | Label | Evidence |
| --- | --- | --- | --- | --- | --- |
| <edge-id> | <node-id> | <node-id> | sync / async / event / data | <protocol, operation, event, or data> | <evidence-ref or decision-id> |

## Structured evidence

| Evidence ref | Kind | Repository-relative path | Line start | Line end | Decision ID | What it supports |
| --- | --- | --- | --- | --- | --- | --- |
| E-01 | file | <path> | <line or None> | <line or None> | None | <groups, nodes, or edges> |
| E-02 | decision | None | None | None | <decision-id> | <groups, nodes, or edges> |

File evidence maps to `kind`, `path`, optional `line_start`, and optional
`line_end`. Decision evidence maps to `kind` and `decision_id`.

## User decisions

| ID | Question | Recommendation presented | Answer | Evidence considered |
| --- | --- | --- | --- | --- |
| <decision-id> | <diagram-changing question> | <recommended choice and tradeoff> | <user answer> | <evidence refs> |

## Assumptions

| ID | Statement | Status | Evidence |
| --- | --- | --- | --- |
| <assumption-id> | <bounded assumption> | pending brief approval / user-approved / rejected | <evidence refs> |

## Unknowns

| ID | Unknown | Diagram impact | Recommendation | Blocking? |
| --- | --- | --- | --- | --- |
| <unknown-id> | <missing or conflicting fact> | <scope, node, group, edge, or label impact> | <least-assumptive choice> | yes / no |

## Render plan

- Output directory: diagrams/<slug>/
- Required artifacts: diagram.json, diagram.py, diagram.svg, render-manifest.json
- Optional QA artifact: diagram.png when available or useful
- Styling: fixed defaults; no theme
- Catalog: installed diagrams==0.25.1 exact paths only
- Group depth: maximum 3
- Existing target: create new / explicitly update <bundle>
- Temporary work: approved JSON enters over stdin; renderer-created spec exists only in renderer-owned hidden staging directly under diagrams/
- Persistence boundary: bundle outputs only under diagrams/<slug>/; never an arbitrary path
- Visual QA: inspect SVG and optional PNG; at most one layout-only retry
- Direction retry: preserve a user override; an agent-selected direction may switch LR to TB or TB to LR

## Approval

No diagram artifact has been written. To authorize the render plan for this
exact revision, reply exactly:

`I approve this Architecture Brief for rendering.`
```

## Approval rules

- Silence, continued discussion, approval of an earlier revision, or a request
  to "show a draft image" is not approval of the current brief.
- Record approval in `diagram.json` only after the explicit approval phrase.
- Approval covers only the listed views, topology, labels, evidence, output
  path, artifacts, and assumptions.
- The exact approval phrase changes every `pending brief approval` assumption
  listed in this revision to `user-approved` in `diagram.json`. Render-mode IR
  must contain no `unresolved` assumption. Never add an assumption after
  approval; revise the brief and request approval again instead.
- Do not request approval while an unknown marked `Blocking? yes` remains.
- One layout-only retry may use the existing approval. It must preserve a
  user-overridden direction; only an agent-selected direction may switch
  `LR` to `TB` or `TB` to `LR`.
- A changed group, node, icon, label, typed connection, assumption, unknown, or
  view boundary requires a new revision and new approval.
- Updating an existing bundle also requires an explicit update request; brief
  approval alone does not authorize replacement.
