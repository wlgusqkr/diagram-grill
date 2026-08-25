# Prelaunch evaluation fixtures

This directory defines 12 fixed architecture cases for comparing a general
agent with the full Diagram Grill workflow. It contains inputs and human-made
oracles only. It does not contain evaluation results or performance claims.

Each fixture has:

- `prompt.md`: the exact task given to both conditions;
- `repo/`: compact, non-executable evidence presented as a read-only project;
- `oracle.json`: observable facts, evidence locations, critical unresolved
  decisions, permitted topology concepts, required connections, forbidden
  unsupported assumptions, and the expected diagram shape.

The agent under evaluation must not receive `oracle.json`. The oracle is for
review after the run.

## Prelaunch 24-run protocol

Run every case once in each of two conditions, for 24 runs total:

1. **Baseline (12 runs):** use the same agent and model without Diagram Grill.
   Give it the condition-neutral `prompt.md` and the fixture's `repo/` directory.
2. **Skill (12 runs):** use the same agent, model, settings, and fixture. Prefix
   the same base prompt with exactly `$diagram-grill\n\n`; this is the sole
   prompt treatment. Complete its question-and-approval flow without adding
   facts that are absent from the case's oracle.

No fixed base prompt may name Diagram Grill. The deterministic runner retains
both the base prompt and the exact condition-specific delivered prompt.

Freeze the model identifier, agent/client version, tool permissions, operating
system, Python version, Graphviz version, dependency lock, Diagram Grill
commit, and run date before starting. Use a fresh workspace and conversation
for every run. Do not carry an answer, generated artifact, or oracle detail
between conditions. Alternate condition order across consecutive cases to
reduce ordering bias.

If a dependency or client fails, preserve that run as a failure; do not silently
rerun it with changed settings. A rerun belongs to a new, separately identified
experiment.

## Raw artifacts

Retain the unedited material from every run under an experiment-specific
results location outside these fixed fixtures:

- case ID, condition, model and client metadata, timestamps, and commit IDs;
- the exact prompt and immutable snapshot identifier for `repo/`;
- full conversation transcript, questions, answers, and tool log;
- Architecture Brief and approval record when the skill condition creates one;
- `diagram.json`, generated Python, SVG, PNG if requested, and render manifest;
- stdout, stderr, exit status, warnings, and failures;
- a reviewer record that cites the relevant oracle items.

Keep failed and partial outputs. Never replace raw artifacts with a cleaned-up
example.

## Review boundary

Review both conditions against the same oracle. Record at least critical
ambiguity coverage, unsupported assumptions, required-connection coverage,
approval fidelity where applicable, build success, visible rendering defects,
and user turns needed to resolve decisions. The oracle defines acceptable
concepts rather than a single pixel-perfect diagram.

Do not publish an aggregate percentage, improvement claim, or winner until all
24 planned runs are complete, their raw artifacts are retained, and the scoring
has been reviewed for every case. Before that point, report only the protocol
and clearly labeled individual observations.

## Deterministic harness

Validate the fixed fixture index, evidence paths, cited line ranges, topology
references, edge types, and non-executable input boundary:

```bash
python evals/harness.py validate-fixtures
```

Record each reviewed run against `run.schema.json`. Artifact paths are relative
to the run record, may not traverse directories, and must point to retained raw
files. A claimed render requires its JSON, manifest, and SVG. A claimed visual
review also requires the reviewed PNG.

```bash
python evals/harness.py score path/to/run.json
python evals/harness.py aggregate --phase prelaunch-24 run-01.json run-02.json
```

The scorer publishes a metric vector, not a weighted score:

- evidence-fact and critical-ambiguity coverage;
- topology-node and required-connection coverage;
- unsupported-assumption count and rate;
- critical decisions covered per question;
- approval fidelity, render success, visual defects, and safety violations.

Strict success requires all critical decisions and connections, at least 80%
of evidence facts and topology concepts, no unsupported assumption or safety
violation, approval before writes, a successful render, and visual QA without a
recorded defect. This definition is fixed before runs begin. The harness
withholds aggregate metrics until every planned case/condition/repeat tuple is
present; failed and partial runs still count as completed protocol observations.

## Rigorous 108-run follow-up

After the public alpha, run 12 cases × 3 conditions × 3 independent repeats:

1. **Baseline:** the general agent receives no Diagram Grill instructions or
   tooling.
2. **Renderer-only:** the agent may use the schema, catalog, validator, and
   renderer, but does not receive the interview, evidence, or approval workflow.
3. **Diagram Grill:** explicitly invoke the complete installed skill.

Use `phase: rigorous-108` and repeats 1 through 3 in each run record. Freeze the
same execution variables as the prelaunch gate, randomize condition order,
start fresh contexts, preserve failures, and publish raw reviewed records with
any aggregate comparison.
