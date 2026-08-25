# Condition-neutral operator protocol

Use this protocol for both conditions in the 24-run prelaunch evaluation. Its
purpose is to keep user decisions deterministic without helping one condition
more than the other.

The checked-in `prompt.md` files are condition-neutral. The Diagram Grill
condition receives exactly `$diagram-grill\n\n` before that base prompt; the
baseline receives the base prompt unchanged. Do not add any other
condition-specific instruction.

The fixed responses are in `operator-answers.json`. The evaluator may read
that file. The agent under evaluation must not receive it, any `oracle.json`,
or information derived from either source.

## Turn protocol

1. Start a fresh workspace and conversation with the case's exact `prompt.md`
   and read-only access to its `repo/` directory.
2. Do not volunteer architecture decisions, hints, corrections, or topology.
3. When the agent asks a question that matches one
   `critical_unresolved_decisions[].id`, reply with that decision's exact
   `answer` from `operator-answers.json`. Match by the question's meaning, not
   by expecting the agent to know the hidden ID.
4. Reveal only the answer for the decision actually asked. Do not bundle
   answers for unasked decisions. If one question clearly asks two indexed
   decisions, return the two exact answers without extra explanation.
5. Give the same fixed answer in baseline and skill conditions. Do not
   paraphrase, elaborate, negotiate, or repair an answer for either condition.
6. If the agent repeats a resolved question, repeat the same exact answer. Log
   the repetition; do not invent a new decision.
7. If the agent asks something not indexed for the case, reply:
   `That is not specified. Keep it explicit as unknown and do not assume it.`
8. If the agent writes artifacts, presents a final answer, or otherwise
   finishes without asking an indexed decision, record that behavior exactly.
   Do not interrupt or rescue the run by supplying the missing answer.

## Architecture Brief approval

Approve only after every indexed decision for the case has been asked and the
current Architecture Brief accurately reflects every fixed answer without
contradicting observed repository evidence. Use this exact approval phrase and
no substitute:

`I approve this Architecture Brief for rendering.`

If the brief omits, changes, or contradicts a revealed answer, do not approve.
Reply only with the exact fixed answer that it failed to preserve and request a
revised brief. A later topology, grouping, label, connection type, assumption,
unknown, or output-plan change requires a revised brief and a new approval.

If the agent requests approval while an indexed decision remains unasked,
withhold approval and record the run at that point. Do not identify the missing
decision or reveal its answer to rescue the run.

If a baseline condition does not use an Architecture Brief, do not introduce
one. The approval phrase is sent only in response to a qualifying brief that
explicitly asks for approval.

## Failure and evidence handling

- Preserve full transcripts, tool logs, stdout, stderr, exit codes, partial
  files, and malformed artifacts. Do not clean up or replace a failed run.
- Do not retry with changed wording, permissions, model settings, or operator
  answers. Any rerun is a separately identified experiment.
- Do not tell the agent which questions the oracle expects, whether a topology
  is scoring well, or which assumptions are forbidden.
- Do not reveal case scores or cross-condition observations during the 24-run
  sequence.
- Do not create aggregate results or performance claims from this operator
  protocol. The review boundary in `README.md` still applies.

## Codex CLI runner

`codex_runner.py` prepares oracle-free workspaces, fixes the 24-run order,
retains raw JSONL, and sends only registered operator text. It does not infer
which decision an agent asked about and it does not adjudicate a run. The human
operator must match the question by meaning and select the corresponding ID.

Create a dedicated runtime outside this repository and outside the results
directory. Installing dependencies here is an evaluator setup step; the skill
still never installs them automatically. System Graphviz must already exist.

```bash
python3 -m venv /absolute/external/path/diagram-grill-eval-venv
/absolute/external/path/diagram-grill-eval-venv/bin/python3 -m pip install \
  -r skills/diagram-grill/requirements.lock
```

Print the frozen order, then prepare one run. `--skill-package` is validated in
both conditions so both use the same runtime checks, but it is copied into the
agent workspace only for `diagram-grill`.

```bash
.venv/bin/python evals/codex_runner.py plan

.venv/bin/python evals/codex_runner.py prepare \
  --results-root /absolute/external/path/diagram-grill-results \
  --experiment prelaunch-2026-08-25 \
  --case 01-checkout \
  --condition baseline \
  --model gpt-5.4 \
  --model-version gpt-5.4-requested-2026-08-25 \
  --runtime-bin /absolute/external/path/diagram-grill-eval-venv/bin \
  --skill-package skills/diagram-grill
```

Preparation makes no model call. It rejects runtime paths inside the source or
results trees, runs the skill preflight with the external `python3`, freezes
installed dependencies, hashes the base and delivered prompts, snapshots the
evidence, and validates that the skill package contains no fixtures, tests, or
oracles.

Launch and resume require an explicit `--execute` acknowledgement because each
command can make a billable model call. The fixed treatment is Codex CLI with
medium reasoning, default service tier, approval policy `never`,
`workspace-write`, and user config and rules ignored.

```bash
RUN=/absolute/external/path/diagram-grill-results/prelaunch-2026-08-25/01-checkout--baseline--r1

.venv/bin/python evals/codex_runner.py launch "$RUN" --execute
.venv/bin/python evals/codex_runner.py reply "$RUN" --decision-id payment-timing --execute
.venv/bin/python evals/codex_runner.py reply "$RUN" --unknown --execute
.venv/bin/python evals/codex_runner.py reply "$RUN" --approve --brief-reviewed --execute
.venv/bin/python evals/codex_runner.py close "$RUN" --reason agent-finished
.venv/bin/python evals/codex_runner.py status "$RUN"
```

Use only the reply that matches the current agent message. Multiple
`--decision-id` flags are allowed only when one agent question clearly asks
those decisions together. Combine decision IDs with `--unknown` only when the
same agent turn also asks an unindexed question; the runner sends the fixed
answers on separate lines. Approval is refused unless every indexed decision
has already been answered and the operator explicitly attests that the current
brief was reviewed. If approval is requested too early, close with
`--reason approval-withheld`; do not reveal the missing answer.

Each run retains `input/` snapshots, the working tree, `state.json`, exact
operator messages, per-turn stdout/stderr/last messages, a raw combined
`transcript.jsonl`, and a turn index. A changed evidence repository or installed
skill is recorded as an integrity violation. Client failures and timeouts are
preserved and never retried in place.
