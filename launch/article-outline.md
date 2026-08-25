# Dev.to and Hashnode article outline

This is a technical-article outline, not a finished cross-post. Adapt the
opening, examples, tags, and conclusion for each publication instead of
posting identical copy.

## Working-title ingredients

- Building and evaluating an Agent Skill
- Evidence-grounded software architecture diagrams
- Approval-gated diagram-as-code generation

Use “evaluating” rather than “improving” until comparative results support an
improvement claim.

## Reader promise

By the end, a reader should understand why architecture clarification is
separate from rendering, how the constrained pipeline works, how it is tested,
and where its limits are. The article should be useful even if the reader never
installs the project.

## Outline

1. **A diagram can be valid and still be wrong**
   - Open with a real example of an unsupported boundary, connection, or
     ownership decision.
   - Explain why visual polish can conceal missing architecture knowledge.
2. **Design goals and non-goals**
   - Cloud and software-system architecture only.
   - Explicit invocation, no theme interview, no general UML replacement.
   - Versioned, reviewable artifacts rather than a one-off image response.
3. **The workflow**
   - Permissioned read-only evidence inspection.
   - Unresolved-decision interview.
   - Evidence-linked Architecture Brief and explicit approval.
   - JSON IR, validation, compilation, rendering, and visual QA.
4. **Why use a constrained IR**
   - Fixed node, group, edge, evidence, and approval fields.
   - Typed connections: `sync`, `async`, `event`, and `data`.
   - Maximum group depth and overview/detail split rules.
5. **Safe compilation and rendering**
   - Exact allowlisted icon imports and visible Generic Blank fallback.
   - Escaped user strings, generated trusted Python, local subprocess limits,
     manifest hashes, and collision protection.
   - Graphviz and Python preflight without automatic installation.
6. **What the skill does not solve**
   - It cannot infer business decisions absent from code and user answers.
   - It does not prove that the approved architecture is operationally sound.
   - It requires local Python, Graphviz, and the pinned dependency.
   - Visual QA is bounded to one layout-only retry.
7. **Evaluation design**
   - Twelve fixed synthetic cases with evidence, hidden oracles, and fixed
     operator answers.
   - Baseline versus complete skill, 24 planned prelaunch runs.
   - Same model and client settings, fresh workspaces, retained failures.
   - Metric vector and strict-success definition without a hidden weighted
     score.
8. **Results and failure analysis**
   - Include this section only after all records are reviewed.
   - Publish the complete metric table, raw-data link, and limitations.
   - Explain at least one failure from each relevant condition.
9. **A version-pinned walkthrough**
   - Exact release, prerequisites, install command, copyable prompt, and output
     bundle.
   - Use a public sample without exposing evaluation oracles to an active run.
10. **What comes next**
    - The fixed 108-run follow-up and opt-in feedback loop.
    - Describe these as plans until they are complete.

## Required figures

- Pipeline diagram: evidence → questions → approved brief → IR → bundle.
- One actual Before/After from a retained baseline/skill pair.
- A readable output-bundle tree.
- Results chart only after the complete reviewed aggregate exists.

## Evidence links to collect

- Public release and immutable commit.
- Schema and compiler source.
- Security boundary documentation.
- Fixture index and evaluation protocol.
- Raw reviewed run records and aggregate output, when complete.
- Client-specific test records for every client named in the article.

## Cross-post discipline

- Use each site's canonical-link mechanism when available.
- Change the opening and discussion prompt for the audience.
- Preserve identical metric definitions and source links across versions.
- Correct every version if a published claim is later found to be wrong.
