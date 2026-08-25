# Evidence and claim checklist

Every public statement should identify its evidence before publication. If the
evidence does not exist or cannot be shared, remove the claim.

## Claim matrix

| Claim type | Minimum evidence | Safe wording before evidence | Never infer from |
| --- | --- | --- | --- |
| Project scope | Current `SKILL.md`, schema, and runtime code | Describe implemented scope only | Roadmap or prompt examples |
| Install command | Fresh signed-out install against the exact public release | `Installation is not yet verified.` | A local-path install |
| Runtime prerequisites | Clean-environment preflight and dependency lock | State documented requirements | Developer machine state alone |
| Output bundle | Integration run retaining JSON, Python, SVG, manifest, and optional PNG | List only retained artifact types | Unit tests alone |
| Approval-before-write behavior | Transcript, tool log, and artifact timestamps from an end-to-end run | Describe the designed workflow | The instruction text alone |
| Security property | Specific test and threat-boundary documentation | `Designed to...` with the boundary | Absence of a reported incident |
| Client compatibility | End-to-end run with client version, model, prompt, transcript, and artifacts | Omit the client | Shared Agent Skills syntax |
| Baseline comparison | Complete paired protocol, reviewed records, and public metric definitions | Describe the protocol only | One selected anecdote |
| Success percentage | Complete planned matrix and reproducible aggregate | Publish no percentage | Partial runs or excluded failures |
| Before/After demo | Actual retained baseline failure and paired skill run | No comparison asset | A staged or reconstructed output |
| Visual quality | Retained PNG plus recorded visual review | State that visual QA was unavailable | Successful SVG generation |
| OSS evaluation | Pinned checkout, retained run, license compliance, and reviewed result | `Three OSS cases are defined for future evaluation.` | Metadata pins alone |
| User count | Opt-in, deduplicated records with a stated definition | `No user count is available.` | Stars, clones, or page views |
| Repeat usage | Opt-in longitudinal record and explicit metric definition | Make no retention claim | Multiple commands in one session |
| Install count | Distribution-channel metric with collection limits and date window | State the source and window | GitHub traffic alone |
| Test count | Current clean CI output for the claimed commit and supported Python matrix | Give no fixed count | An earlier local run |

## Evaluation-specific gates

- `12 fixtures` means the repository defines 12 fixed synthetic cases. It does
  not mean 12 successful evaluations or 12 real production systems.
- `24 prelaunch runs` may be stated as a plan until all 12 baseline and 12
  Diagram Grill records exist. Do not publish an aggregate before completion.
- `108 follow-up runs` is a post-alpha protocol: 12 cases × three conditions ×
  three repeats. Do not describe it in the past tense until complete.
- Failed, partial, dependency-error, and client-error runs stay in the
  denominator defined by the protocol.
- Any rerun with changed wording, permissions, versions, or answers is a new
  experiment, not a replacement.
- Publish metric definitions, raw reviewed records, and the exact commit with
  every aggregate.

## Compatibility record requirements

For each named client, retain:

- client name and exact version;
- model identifier and relevant settings;
- operating system and runtime versions;
- installation mode and installed-file inventory;
- exact representative prompt and explicit invocation;
- full transcript and tool log;
- Architecture Brief and approval record;
- generated artifact bundle and exit status;
- visual-QA record or an explicit statement that it was unavailable;
- reviewer and test date.

Passing installation alone is not compatibility.

## Claim record template

Create one record per material claim before publication:

```text
Claim ID:
Exact public wording:
Channel and asset:
Release or commit:
Evidence paths or public URLs:
Metric definition, if any:
Evidence date and environment:
Reviewer:
Known limitations shown beside the claim:
Approved for publication: yes / no
```

## Prohibited shortcuts

- Do not translate planned numbers into completed results.
- Do not call synthetic fixtures “real-world users” or “production cases.”
- Do not turn a successful render into proof of architectural correctness.
- Do not turn an MIT dependency into affiliation or endorsement.
- Do not turn a standards-compatible file into a tested-client claim.
- Do not quote private feedback or identify a person without explicit consent.
- Do not add hidden telemetry to fill a measurement gap.
- Do not silently change a claim after publication; issue a visible correction.

## Final launch audit

- Every number has a public definition, date window, and evidence link.
- Every named client has a complete compatibility record.
- The demo comes from a real retained baseline failure.
- The Before/After uses the same frozen case and settings.
- Limitations appear near the relevant claim.
- The install command was tested against the exact public release.
- No users, testimonials, or adoption are implied without opt-in evidence.
- HN and Reddit prose was written by the user.
- The channel action has separate explicit approval.
