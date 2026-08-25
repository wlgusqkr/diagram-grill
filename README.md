# diagram-grill

> Architecture diagrams that ask before they assume.

[한국어 요약](README.ko.md)

`diagram-grill` is an open-source Agent Skill for cloud and software-system
architecture. It inspects repository evidence, asks about decisions that code
cannot answer, and turns an approved architecture brief into a versioned,
reviewable diagram-as-code bundle powered by
[`mingrammer/diagrams`](https://github.com/mingrammer/diagrams).

The repository is under active public-alpha development. Compatibility and
performance claims will be added only after their raw test results are
published.

## Try it

Invoke the skill explicitly from a repository that contains architecture
evidence:

```text
$diagram-grill

Inspect the Terraform and Kubernetes files in this repository. Ask me about
any architecture decisions the files cannot establish, then render the
approved system architecture.
```

The skill is designed to present an evidence-linked Architecture Brief first
and to write no diagram until you approve that brief with the exact approval
step it requests. This gate is an implemented instruction and validator
contract; its end-to-end behavior is not claimed as evaluated yet.

## Intended workflow

1. Inspect relevant code, Terraform, Kubernetes, and Docker Compose files.
2. Ask only the architecture questions that remain unresolved.
3. Present an evidence-linked architecture brief for approval.
4. Validate a constrained JSON specification.
5. Generate Python, SVG, and a versioned render manifest.

The result is a non-overwriting bundle:

```text
diagrams/<slug>/
├── diagram.py
├── diagram.svg
├── diagram.json
├── render-manifest.json
└── diagram.png (optional)
```

Rendering uses fixed, readable defaults. There is no theme interview or theme
configuration surface.

## Scope

`diagram-grill` is intentionally narrow: cloud and software-system
architecture. It is not an ERD, sequence-diagram, UML, or general flowchart
generator. Exact branded icons require observed repository evidence or explicit
user approval; otherwise the output uses a visibly labeled generic node.

## Runtime prerequisites

- Python 3.9+
- Graphviz `dot`
- Node.js 22.20+ with `npx` for the documented installer only
- Python dependencies pinned in
  [`skills/diagram-grill/requirements.lock`](skills/diagram-grill/requirements.lock)
  (including `diagrams==0.25.1` and `graphviz==0.20.3`)

## Install

After the first public tag, install a copied global skill:

```sh
npx skills@1.5.23 add \
  https://github.com/wlgusqkr/diagram-grill/tree/v0.1.0-alpha/skills/diagram-grill \
  --skill diagram-grill --global --copy
```

Project-local installs are unsupported. Keep the canonical installed skill
directory outside every target repository and workspace so repository content
cannot replace the instructions or renderer. The CLI refuses overlapping
paths. The source URL and installer version above are pinned for the planned
`v0.1.0-alpha` release. This command installs only the Agent Skill files; it
does not install Python or Graphviz. See the
[`skills` CLI documentation](https://github.com/vercel-labs/skills#install-a-skill)
for the installer source formats and flags.

## Evaluation status

The repository includes 12 fixed synthetic architecture cases and a frozen,
alternating 24-run baseline-versus-skill protocol under [`evals/`](evals/).
Those files make the planned evaluation reviewable; they are not a claim that
the runs have been completed or that the skill improves a metric.

The billable runner requires a dedicated API project key with its own budget
and rate limit. It accepts the key only through stdin and passes it to a
disposable fixed Responses proxy; the evaluated agent receives no credential,
and the runner does not persist the key in arguments, environment variables,
state, or logs. Until all 24 runs close without integrity violations and the
retained outputs are reviewed, this README will not publish an aggregate
result, a Before/After comparison, or a demo GIF.

## Client compatibility

No client is claimed as tested yet. A client will be listed only after an
end-to-end install, invocation, evidence inspection, approval, render, and
artifact review is recorded with its exact version. Sharing the Agent Skills
file format alone does not establish compatibility.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for development checks and
[SECURITY.md](SECURITY.md) for the trust boundary and private reporting path.

## License

MIT. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream
attribution. The installable runtime is isolated under
[`skills/diagram-grill/`](skills/diagram-grill/) so development tests and
evaluation oracles are never copied into a user's agent directory.
