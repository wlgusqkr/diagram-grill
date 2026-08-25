# Reddit experience-report outline

**Not submission-ready prose. The user must write the final title and post in
their own words for the selected community. Do not post this file or reuse one
final post across multiple communities.**

## Community gate

- Choose an English-language community whose current scope includes software
  architecture, developer tools, Python, or Agent Skills.
- Read its current self-promotion, link, flair, account-age, and cross-post
  rules immediately before drafting.
- Disclose that the user built the project.
- Prefer a complete experience report hosted in the post over a link-only
  submission.
- Ask moderators first if the rules are ambiguous.

## Title ingredients

- The concrete experiment or debugging problem.
- The fixed number of cases or runs only after those runs are complete.
- A promise to discuss where the approach worked and failed.
- No launch language, superlatives, or unexplained “AI Agent Skill” jargon.

The user writes the final title. Do not convert these ingredients into a
ready-to-post headline.

## Post outline for the user

1. **The recurring problem**
   - Describe one real moment when a diagram looked confident but hid an
     unresolved architecture decision.
2. **The hypothesis**
   - Ask whether an evidence and approval workflow improves the process, not
     whether a prettier renderer wins.
3. **What was built**
   - Briefly describe Diagram Grill and the narrow supported diagram scope.
4. **Experiment setup**
   - Same agent, requested model identifier, invariant execution fingerprint,
     fresh workspaces, fixed prompts, baseline versus explicit skill
     invocation, condition-neutral answers, and fixed oracles.
   - Say whether the model version was provider-reported or only the requested
     identifier; do not describe an identifier and date as immutable weights.
   - Report source/session isolation, teardown, protocol-hash, and artifact-hash
     integrity checks.
   - State 12 cases and 24 runs only when all 24 records are complete.
5. **Observed outcomes**
   - Use a compact table populated directly from reviewed run records.
   - Report metric definitions beside the values.
   - Include failed and partial runs.
6. **One useful example**
   - Show the actual baseline failure and paired skill artifact selected for
     the demo.
7. **Where it failed**
   - Explain at least one real limitation, defect, or unresolved case.
8. **What changed in the design**
   - Tie any change to an issue, test, or reviewed observation.
9. **Reproduction**
   - Link the repository, exact release, protocol, raw records, prerequisites,
     install command, and copyable prompt.
10. **Discussion question**
    - Ask for experiences with architecture-decision capture or evaluation
      design, not for stars.

## Results table template

Leave cells empty until a reviewed aggregate exists.

| Metric | Baseline | Diagram Grill | Definition and evidence |
| --- | ---: | ---: | --- |
| Primary task success |  |  | Condition-neutral definition fixed before runs |
| Evidence-fact coverage |  |  |  |
| Critical-ambiguity coverage |  |  |  |
| Required-connection coverage |  |  |  |
| Unsupported assumptions |  |  |  |
| Render success |  |  |  |
| Human-reviewed visual defects |  |  | Human review of retained SVG or PNG |
| Approval fidelity |  |  | Separate process metric; not primary success |
| Agent visual QA |  |  | Separate from human review |
| Diagram Grill bundle completeness | n/a |  | JSON, Python, manifest, and SVG retained |

## Do not claim

- Human testers, repeat users, community adoption, or testimonials that do not
  exist.
- A winner from selected anecdotes or an incomplete run matrix.
- “Real-world tested” from synthetic fixtures.
- Successful evaluation of the three pinned OSS cases before those runs exist.
- Compatibility with a client because it recognizes `SKILL.md` in principle.
- An exact copy of the X or Show HN message.
