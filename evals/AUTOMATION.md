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

`codex_runner.py` prepares oracle-free inputs, fixes the 24-run order, retains
bounded raw evidence, and sends only registered operator text. It does not
infer which decision an agent asked about and it does not adjudicate a run.
The human operator must match the question by meaning and select the
corresponding ID.

Build both pinned, non-root images. The agent image contains Codex and the
diagram runtime but no credential. Of the two evaluation containers, only the
smaller proxy container receives the dedicated project key. The host runner
accepts it only through stdin and does not persist it. The `evals/` build
context is allowlisted by `.dockerignore`; fixtures, oracles, and operator
answers cannot enter either image layer. Preparation accepts immutable local
image IDs, never mutable tags.

```bash
set -eu

REPOSITORY_ROOT=/absolute/path/to/diagram-skill
BASE_PYTHON=/absolute/trusted/python3
EVALUATOR_PREFIX=/absolute/external/path/diagram-grill-evaluator-python
EVALUATOR_PYTHON="$EVALUATOR_PREFIX/bin/python"
RESULTS_ROOT=/absolute/external/path/diagram-grill-results
SESSION_STATE_ROOT=/absolute/external/path/diagram-grill-session-state
DOCKER_EXECUTABLE=/Applications/Docker.app/Contents/Resources/bin/docker

if [ -e "$EVALUATOR_PREFIX" ] || [ -L "$EVALUATOR_PREFIX" ]; then
  printf '%s\n' 'EVALUATOR_PREFIX must be a new, nonexistent path.' >&2
  exit 1
fi

umask 077
"$BASE_PYTHON" -I -S -m venv "$EVALUATOR_PREFIX"
/bin/chmod 700 "$EVALUATOR_PREFIX"
"$EVALUATOR_PYTHON" -I -m pip install \
  -r "$REPOSITORY_ROOT/skills/diagram-grill/requirements.lock"

"$DOCKER_EXECUTABLE" build \
  --file "$REPOSITORY_ROOT/evals/Dockerfile.codex" \
  --tag diagram-grill-codex-eval:0.149.1 \
  "$REPOSITORY_ROOT/evals"

"$DOCKER_EXECUTABLE" build \
  --file "$REPOSITORY_ROOT/evals/Dockerfile.proxy" \
  --tag diagram-grill-responses-proxy:1 \
  "$REPOSITORY_ROOT/evals"

AGENT_IMAGE=$("$DOCKER_EXECUTABLE" image inspect \
  diagram-grill-codex-eval:0.149.1 --format '{{.Id}}')
PROXY_IMAGE=$("$DOCKER_EXECUTABLE" image inspect \
  diagram-grill-responses-proxy:1 --format '{{.Id}}')
/bin/mkdir -p "$RESULTS_ROOT" "$SESSION_STATE_ROOT"
/bin/chmod 700 "$RESULTS_ROOT" "$SESSION_STATE_ROOT"

run_eval() {
  "$EVALUATOR_PYTHON" -I -S \
    "$REPOSITORY_ROOT/evals/evaluator_bootstrap.py" \
    --evaluator-prefix "$EVALUATOR_PREFIX" \
    --results-root "$RESULTS_ROOT" \
    --session-state-root "$SESSION_STATE_ROOT" \
    -- "$@"
}
```

The dedicated evaluator prefix, results root, and session-state root must be
absolute, owner-controlled paths outside this repository and outside one
another. Keep the two roots mode `0700`; the runtime also rejects foreign
ownership and extended ACLs. `BASE_PYTHON`, its prefixes, and the resulting
evaluator prefix must also stay outside the repository and both roots.
Each prepared `input` parent remains current-user-owned mode `0700` with no
extended ACL. Its read-only bind subtrees use `0755` directories and `0644`
files so the fixed non-root container user can traverse them even with Linux
user-namespace remapping; the private parent prevents other host users from
resolving those paths. Host and container manifests are compared immediately
around launch and again before snapshot commit.
`EVALUATOR_PREFIX` must name a new path created only for this evaluation;
never reuse a project or general-purpose virtual environment. The later
bootstrap directly inserts vetted library roots and does not process `.pth` or
`sitecustomize`, but the one-time `-I -m pip` provisioning command starts
Python with `site` enabled. Reusing an existing environment could therefore
execute its pre-existing startup hooks during installation.

`BASE_PYTHON` and `DOCKER_EXECUTABLE` must be absolute, operator-verified
executables; do not resolve either through `PATH`. Always use the `-I -S`
bootstrap above rather than executing `codex_runner.py` directly. It validates
the raw and resolved interpreter, base interpreter, every Python prefix, and
the manually selected
`purelib`/`platlib` roots before it imports evaluator code or reads stdin. It
adds those library directories directly and never processes `site`, `.pth`,
`sitecustomize`, `PYTHONPATH`, or a repository-local virtual environment.

Do not point the runner at an existing `CODEX_HOME`, `auth.json`, or
general-purpose API key. Create a dedicated OpenAI API project first, set a
budget and rate limit for the 24-run evaluation, and keep its key out of shell
history. The runner has no environment-variable or auth-file fallback.

Print the frozen order, then prepare one run. `--skill-package` is validated in
both conditions, but it is copied into the agent workspace only for
`diagram-grill`.

```bash
run_eval plan

run_eval prepare \
  --experiment prelaunch-2026-08-25 \
  --case 01-checkout \
  --condition baseline \
  --model gpt-5.4 \
  --model-version gpt-5.4-requested-2026-08-25 \
  --skill-package "$REPOSITORY_ROOT/skills/diagram-grill" \
  --agent-image "$AGENT_IMAGE" \
  --proxy-image "$PROXY_IMAGE" \
  --docker-executable "$DOCKER_EXECUTABLE" \
  --docker-socket /Users/you/.docker/run/docker.sock \
  --confirm-dedicated-project-key
```

Preparation makes no model call. It fails unless Docker and its daemon are
available through the explicit trusted executable and Unix socket, both exact
image IDs exist, Docker Engine 28 or newer is available, and the images declare
their expected non-root contracts. Linux hosts must provide `/usr/bin/getfacl`
or `/bin/getfacl` so trusted paths and mount sources can be checked fail-closed.
Preparation also uses Codex's local `skills/list` interface to verify the exact
mounted treatment path, then runs the nested-bubblewrap isolation probe: fresh
PID and `/proc`
namespaces, blocked tool network, writable bounded workspace, read-only
evidence-repository and skill bind mounts whose files are actually opened by
the image's non-root user (including on native Linux), denied Codex home, and
no credential or auth file. The pinned wrapper permits only Codex's exact
bubblewrap capability probes, requires the namespace and capability-drop
contract for payload execution, and adds a private inner `/tmp`. It then
freezes the dependency inventory, hashes the prompts, snapshots the evidence,
and validates that the skill package contains no fixtures, tests, or oracles.

Launch and resume require three independent gates because each command can make
a billable model call: `--execute`, `--confirm-dedicated-project-key`, and the
key itself through `--credential-stdin`. Re-enter or pipe the key from a secret
manager for every turn. Never put it in an argument, exported environment
variable, file, or command substitution. The fixed treatment uses medium
reasoning, default service tier, approval policy `never`, a `workspace-write`
sandbox, disabled tool network and web search, and ignored user config/rules.

```bash
RUN="$RESULTS_ROOT/prelaunch-2026-08-25/01-checkout--baseline--r1"

# Pipe one key line from an interactive prompt or secret manager each time.
read -r -s DIAGRAM_GRILL_EVAL_KEY
printf '\n'
printf '%s\n' "$DIAGRAM_GRILL_EVAL_KEY" | \
  run_eval launch "$RUN" \
  --execute --credential-stdin --confirm-dedicated-project-key
unset DIAGRAM_GRILL_EVAL_KEY

# Repeat the protected prompt for exactly one matching reply.
read -r -s DIAGRAM_GRILL_EVAL_KEY
printf '\n'
printf '%s\n' "$DIAGRAM_GRILL_EVAL_KEY" | \
  run_eval reply "$RUN" \
  --decision-id payment-timing \
  --execute --credential-stdin --confirm-dedicated-project-key
unset DIAGRAM_GRILL_EVAL_KEY

# Other reply choices replace --decision-id; they are not additional turns:
#   --unknown
#   --approve --brief-reviewed
run_eval close "$RUN" --reason agent-finished
run_eval status "$RUN"
```

Only run the reply that matches the current agent message, with a fresh key
line on stdin.

Use only the reply that matches the current agent message. Multiple
`--decision-id` flags are allowed only when one agent question clearly asks
those decisions together. Combine decision IDs with `--unknown` only when the
same agent turn also asks an unindexed question; the runner sends the fixed
answers on separate lines. Approval is refused unless every indexed decision
has already been answered and the operator explicitly attests that the current
brief was reviewed. If approval is requested too early, close with
`--reason approval-withheld`; do not reveal the missing answer.

## Human review and immutable run record

Finalize only after closing the run. A human reviewer must inspect the retained
transcript, artifacts, repository evidence, and the case oracle, then supply
both the adjudication and the artifact mapping. The runner does not infer
questions, facts, topology, success, approval timing, render success, or visual
quality from agent output.

Create a review input outside the agent workspace with exactly these top-level
fields. This neutral skeleton records no successful outcome; replace values
only when the retained evidence supports them.

```json
{
  "schema_version": "1.0",
  "artifacts": {
    "transcript": "review/transcript.jsonl"
  },
  "adjudication": {
    "reviewer": "human-reviewer-id",
    "reviewed_at": "2026-08-25T21:00:00+09:00",
    "status": "partial",
    "evidence_fact_ids": [],
    "critical_decision_ids": [],
    "topology_node_concepts": [],
    "required_connection_ids": [],
    "unsupported_assumptions": [],
    "architecture_claim_count": 0,
    "question_count": 0,
    "approval_requested_before_write": false,
    "files_written_before_approval": false,
    "render_succeeded": false,
    "human_visual_review_performed": false,
    "human_visual_defects": [],
    "agent_visual_qa_performed": false,
    "safety_violations": []
  }
}
```

Every artifact value is a path relative to the run directory. It must name a
retained regular file, not a symlink, absolute path, or traversal. Map the
reviewer's assembled transcript explicitly. When a viewable result exists,
map `primary_diagram` to its SVG or PNG. For a Diagram Grill bundle, retain and
map `architecture_brief`, `diagram_json`, `diagram_python`, `render_manifest`,
and `diagram_svg` or `diagram_png` as applicable. Do not add a field for an
artifact that was not actually retained.

Set `human_visual_review_performed` only after a person opens the retained
primary SVG or PNG and inspects it. Record visible defects in
`human_visual_defects`. An agent's own render inspection is recorded
separately in `agent_visual_qa_performed` and never substitutes for human
review. Approval and complete-bundle fields remain process metrics; they do
not determine the condition-neutral primary success outcome.

```bash
run_eval finalize-review "$RUN" \
  --review /absolute/external/path/review-input.json

"$EVALUATOR_PYTHON" "$REPOSITORY_ROOT/evals/harness.py" \
  score "$RUN/reviewed-run.json"
```

`finalize-review` accepts closed runs only. It recomputes normalized execution
metadata and the execution fingerprint, aggregates isolation and teardown over
every indexed turn, pins all eight protocol hashes to the state captured at
preparation, hashes every mapped artifact, and validates the result through
the harness. Protocol drift, turn gaps, unsafe artifact paths, missing files,
or schema-invalid human judgments fail before publication. A dirty pilot is
retained as a source-integrity violation and is not claim-ready. The validated
`reviewed-run.json` is installed atomically and is never overwritten; correct
the source evidence by preparing a new run, not by replacing the record.

Each run retains immutable `input/` snapshots, bounded workspace/session
snapshots, `state.json`, exact operator messages, capped per-turn
stdout/stderr/last messages, and a turn index. A changed repository or skill
snapshot is an integrity violation. Client failures and timeouts are preserved
and never retried in place. A cap violation fails the turn explicitly; the
runner never silently truncates evidence and calls it successful.
Immediately before any billable dispatch, the runner claims a per-turn marker
with `O_EXCL`, fsyncs both the file and `raw/` directory, and keeps the audit
marker permanently. Only a durably published turn index and `state.json` may
transition it to `completed`. An `in-progress` marker after a crash makes that
run permanently non-resumable; prepare a new run rather than risk a duplicate
request or contaminated sample.

Every turn creates a fresh credential-free agent, a fresh proxy, and two fresh
networks. The agent has only an internal network route to the proxy; the proxy
binds only its internal interface and alone also joins an egress network. The
internal bridge uses Engine 28's isolated IPv4/IPv6 gateway modes; active probes
require no agent default route, public DNS result, or host alias. Tool
subprocesses run in a nested network
namespace and cannot reach either the proxy or the internet. Writable data is
limited to size/inode-capped tmpfs at `/workspace`, `/codex-home`, and `/tmp`.
The repository and treatment skill are nested read-only mounts. No host path is
mounted read-write, and fixtures, operator answers, results, credentials, and
the Docker socket are never mounted.

After Codex exits, the container must be quiescent before link-free bounded tar
exports become immutable content-addressed workspace and session generations.
One fsynced journal pointer publishes the pair for an exact turn and Codex
session; a state/journal mismatch refuses resume, while a crash before pointer
replacement leaves the prior pair current. Archive names, Unicode/case aliases,
file types, counts, byte sizes, credentials, and agent-created skill directories
are revalidated on import and export. The dedicated key, a per-turn sentinel,
and a random per-turn proxy capability are scanned out of all agent output
before persistence. The capability is also required on every proxied Responses
request, whose shape is pinned to the evaluated Codex version. Agent, proxy,
and both networks are labeled and force-removed on success, nonzero exit,
timeout, interrupt, and export failure. PID 1 also enforces a finite watchdog;
after an uncatchable host crash, the next serialized evaluator start scavenges
all stale labeled resources. A resumed turn imports only the journal-selected,
credential-free session pair into a new container.
