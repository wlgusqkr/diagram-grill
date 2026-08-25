# Security policy

## Supported versions

Security fixes are applied to the latest released version. During the public
alpha, no older release line is maintained.

## Reporting a vulnerability

Please use GitHub's private security-advisory flow instead of opening a public
issue. Include the affected commit, a minimal reproduction, and the expected
security boundary.

## Security boundary

`diagram-grill` treats repository contents and user prompts as untrusted data.
It must not execute Python found in a repository, download remote icons, expose
environment secrets, write outside the selected workspace, or create files
before the user approves the architecture brief.

The project compiles a constrained JSON document into generated Python whose
imports, paths, labels, and Graphviz attributes are allowlisted. Generated
code is still executed locally, so users should inspect proposed changes and
run the skill with ordinary least-privilege workspace permissions.

