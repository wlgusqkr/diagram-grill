# diagram-grill

> Architecture diagrams that ask before they assume.

`diagram-grill` is an open-source Agent Skill for cloud and software-system
architecture. It inspects repository evidence, asks about decisions that code
cannot answer, and turns an approved architecture brief into a reproducible
diagram-as-code bundle powered by
[`mingrammer/diagrams`](https://github.com/mingrammer/diagrams).

The repository is under active public-alpha development. Compatibility and
performance claims will be added only after their raw test results are
published.

## Intended workflow

1. Inspect relevant code, Terraform, Kubernetes, and Docker Compose files.
2. Ask only the architecture questions that remain unresolved.
3. Present an evidence-linked architecture brief for approval.
4. Validate a constrained JSON specification.
5. Generate Python, SVG, and a versioned render manifest.

## Runtime prerequisites

- Python 3.9+
- Graphviz `dot`
- Python dependencies pinned in
  [`skills/diagram-grill/requirements.txt`](skills/diagram-grill/requirements.txt)

The future `npx skills add wlgusqkr/diagram-grill` command installs the Agent
Skill files; it does not install Python or Graphviz.

## License

MIT. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream
attribution. The installable runtime is isolated under
[`skills/diagram-grill/`](skills/diagram-grill/) so development tests and
evaluation oracles are never copied into a user's agent directory.
