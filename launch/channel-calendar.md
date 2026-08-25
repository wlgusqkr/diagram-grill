# Gated channel calendar

All dates are relative to the approved public-alpha date. A date is a planning
slot, not authorization to publish. Skip a slot if its gate is incomplete.

## Before D-7: evidence gate

- Freeze the commit, model identifier, client version, permissions, operating
  system, Python version, Graphviz version, and Python dependency versions.
- Complete the 12 baseline and 12 Diagram Grill prelaunch runs with fresh
  workspaces and condition-neutral operator answers.
- Retain unedited transcripts, tool logs, partial outputs, failures, reviewed
  run records, and render artifacts.
- Validate and score every record against the fixed oracle. Do not publish an
  aggregate until the complete 24-run matrix passes the harness completeness
  gate.
- Identify a real baseline failure suitable for the demo. Use it only if the
  paired skill run is also retained and reviewed under the fixed protocol.
- Run one representative end-to-end prompt in each client that may be named
  publicly. Omit every unverified client from launch copy.
- Confirm that the public repository contains no secrets, private paths,
  evaluation oracles in the installable skill directory, or unpublished raw
  user data.

## D-7: internal release candidate

- Freeze the public-alpha candidate and rerun CI from a clean checkout.
- Prepare the GitHub landing assets: one-sentence problem, verified demo,
  install command, copyable prompt, actual Before/After, limitations, license,
  and evidence links.
- Verify the README's release-tagged, version-pinned `npx skills` command and
  confirm that it installs only the runtime skill directory from the public
  candidate.
- Prepare release notes that distinguish implemented behavior, evaluated
  behavior, and planned work.
- Do not invite or claim human beta users; this launch uses an automated gate.

## D-3: demo and copy freeze

- Produce the 20–30 second demo from retained raw runs according to
  [`demo-storyboard.md`](demo-storyboard.md).
- Check every caption against the reviewed run record.
- Prepare the X draft and channel outlines, then run the claim audit.
- Publishing a teaser at this stage requires separate approval. Otherwise keep
  all assets local.

## D-Day: GitHub and X

Only after explicit approval:

- Make the repository public and verify the public URL from a signed-out
  session.
- Create the public-alpha release and verify its install command.
- Add only the topics cleared in [`github-topics.md`](github-topics.md).
- Publish the approved X post with the verified demo and repository link.
- Record the publication time and immutable release or commit identifier.
- Check installation from a fresh directory after publication.

## D+1: ecosystem discovery

Only after separate approval for each external action:

- Open a focused pull request to a relevant Awesome Agent Skills list.
- Use the list's contribution format and state only verified compatibility.
- Use an official or community Show and Tell area only when self-promotion is
  allowed by its current rules.
- Do not paste the X copy into these channels.

## D+2: Show HN

- Recheck the current [Show HN guidelines](https://news.ycombinator.com/showhn.html).
- The user writes the final title and body using
  [`show-hn-outline.md`](show-hn-outline.md).
- Confirm that a signed-out reader can install and run the project before
  submission.
- Include limitations and a direct way to try the project.
- Submission requires separate approval immediately before posting.

## D+4: Reddit

- Select a relevant English-language community and recheck its current rules,
  self-promotion limits, title format, and cross-post policy.
- The user writes a community-specific experience report using
  [`reddit-outline.md`](reddit-outline.md).
- Include an actual failure or limitation; do not post only a repository link.
- Submission requires separate approval immediately before posting.

## D+7: evidence follow-up

- Run the fixed 108-run follow-up only under its documented protocol: 12 cases,
  three conditions, and three independent repeats.
- Publish aggregate comparisons only when the matrix is complete, raw reviewed
  records are available, and the exact metric definitions accompany the data.
- If the matrix is incomplete, publish no aggregate and no winner claim.
- A technical article may describe the implementation before results exist,
  but its evaluation section must remain explicitly pending.

## D+14: evidence-backed iteration

- Release `v1.1` only if there are concrete fixes or improvements to ship.
- Attribute changes to issue links or opt-in feedback only when those records
  exist and can be shared.
- Do not imply a user community, repeat usage, or adoption from stars alone.
- Publish a changelog post only after its claims pass the evidence checklist.

## Per-channel final check

- The post has one audience-specific problem statement.
- The demo, result, client, and user claims have evidence links.
- The install command works against the exact public release.
- Limitations are visible without opening another page.
- Links use the public repository or immutable evidence, not local files.
- The action has explicit approval and complies with the channel's current
  rules.
- Publication time and URL are recorded for follow-up measurement.
