# Condition-neutral operator protocol

Use this protocol for both conditions in the 24-run prelaunch evaluation. Its
purpose is to keep user decisions deterministic without helping one condition
more than the other.

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
