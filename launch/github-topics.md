# GitHub topics

GitHub permits up to 20 topics on a repository. Keep the initial list focused
and add client-specific topics only after verified compatibility. Current
topic rules are documented in
[GitHub Docs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics).

## Base topics

These describe the repository without making a client-compatibility claim:

1. `agent-skills`
2. `developer-tools`
3. `architecture`
4. `software-architecture`
5. `cloud-architecture`
6. `architecture-diagrams`
7. `diagram-as-code`
8. `architecture-as-code`
9. `infrastructure-as-code`
10. `graphviz`
11. `python`
12. `terraform`
13. `kubernetes`
14. `docker-compose`
15. `devops`

## Compatibility-gated topics

Add each only after an end-to-end test for that client is retained and its
supported status is stated in the repository:

- `codex-skills`
- `claude-code-skills`
- `cursor-skills`

Do not add a client topic merely because the client can parse an Agent Skills
file. Installation, explicit invocation, evidence inspection, approval gating,
rendering, and artifact reporting must all be exercised in the named client.

## Publication checklist

- Use no more than 20 total topics.
- Prefer existing topic spellings when GitHub suggests a canonical topic.
- Keep every topic relevant to implemented behavior.
- Recheck conditional topics whenever the supported-client table changes.
- Do not add unrelated high-traffic AI topics for discovery alone.
