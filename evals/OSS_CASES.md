# Pinned open-source case studies

These three metadata-only cases extend the synthetic fixtures with established
public architectures. They are candidates for a separately controlled
evaluation; they are not evidence that Diagram Grill, a baseline agent, or any
client has run successfully against the repositories.

No upstream source is vendored here. Each case pins a full Git commit and an
exact evidence allowlist in [`oss-cases.json`](oss-cases.json). Branch names,
tags, GitHub archive URLs, and a repository's current `HEAD` are not evaluation
inputs.

## Selected cases

| Case | Coverage | Repository and immutable pin | Upstream license |
| --- | --- | --- | --- |
| `oss-01-online-boutique` | Kubernetes microservices | [GoogleCloudPlatform/microservices-demo](https://github.com/GoogleCloudPlatform/microservices-demo) at [`72ba613a05f7fcee51cf1d0badff401b6ae7074d`](https://github.com/GoogleCloudPlatform/microservices-demo/commit/72ba613a05f7fcee51cf1d0badff401b6ae7074d) | [Apache-2.0](https://github.com/GoogleCloudPlatform/microservices-demo/blob/72ba613a05f7fcee51cf1d0badff401b6ae7074d/LICENSE) |
| `oss-02-terraform-foundation` | Terraform and cloud infrastructure | [terraform-google-modules/terraform-example-foundation](https://github.com/terraform-google-modules/terraform-example-foundation) at [`0a96d838801082e4ab60f9047fa445b1afebc47b`](https://github.com/terraform-google-modules/terraform-example-foundation/commit/0a96d838801082e4ab60f9047fa445b1afebc47b) | [Apache-2.0](https://github.com/terraform-google-modules/terraform-example-foundation/blob/0a96d838801082e4ab60f9047fa445b1afebc47b/LICENSE) |
| `oss-03-voting-app` | Docker Compose, Swarm, and Kubernetes | [dockersamples/example-voting-app](https://github.com/dockersamples/example-voting-app) at [`63e9150ca17af4ed05880d4245e486481f73fcb4`](https://github.com/dockersamples/example-voting-app/commit/63e9150ca17af4ed05880d4245e486481f73fcb4) | [Apache-2.0](https://github.com/dockersamples/example-voting-app/blob/63e9150ca17af4ed05880d4245e486481f73fcb4/LICENSE) |

The Online Boutique case provides a polyglot service graph and Kubernetes
runtime evidence. The Terraform case uses one coherent hub-and-spoke slice of
the larger staged foundation instead of treating every optional module as
deployed. The Voting App case keeps the application topology small while
exposing meaningful differences between three deployment descriptions.

## Reproducible retrieval

Use a disposable checkout outside this repository. Set `case_id` to one of the
IDs above, then resolve its repository and commit from the checked-in metadata:

```bash
case_id=oss-01-online-boutique
case_repo="$(jq -r --arg case_id "$case_id" \
  '.cases[] | select(.id == $case_id) | .repository_url' \
  evals/oss-cases.json)"
case_sha="$(jq -r --arg case_id "$case_id" \
  '.cases[] | select(.id == $case_id) | .commit_sha' \
  evals/oss-cases.json)"
case_checkout="$(mktemp -d)"

git -C "$case_checkout" init
git -C "$case_checkout" remote add origin "$case_repo"
git -C "$case_checkout" fetch --depth=1 origin "$case_sha"
test "$(git -C "$case_checkout" rev-parse 'FETCH_HEAD^{commit}')" = "$case_sha"
git -C "$case_checkout" checkout --detach "$case_sha"
```

This fetches the named commit, not a moving branch or tag. Record the Git
version and object format used by the evaluation environment. Keep the
checkout read-only during evidence inspection, and remove it after retaining
the run's provenance and checksums outside these fixed metadata files.

## Verify the allowlist

Every allowlisted file records the Git blob OID observed in the pinned commit.
Verify all of them before exposing the checkout to an agent:

```bash
jq -r --arg case_id "$case_id" \
  '.cases[] | select(.id == $case_id) | .evidence_allowlist[] |
   [.git_blob_oid, .path] | @tsv' \
  evals/oss-cases.json |
while IFS=$'\t' read -r expected_oid evidence_file; do
  actual_oid="$(git -C "$case_checkout" rev-parse \
    "${case_sha}:${evidence_file}")"
  test "$actual_oid" = "$expected_oid" || {
    printf 'blob mismatch: %s\n' "$evidence_file" >&2
    exit 1
  }
done
```

Apply the same comparison to `upstream_license.path` and
`upstream_license.git_blob_oid`. A missing path, extra path, type change, or OID
mismatch invalidates the prepared case. Do not silently substitute a nearby
commit.

## Produce a portable checksum manifest

Git blob OIDs bind the files to the pinned Git tree. Also retain a sorted
SHA-256 manifest so raw evaluation artifacts can be checked without depending
on Git's object format:

```bash
jq -r --arg case_id "$case_id" \
  '.cases[] | select(.id == $case_id) | .evidence_allowlist[] |
   [.path] | @tsv' \
  evals/oss-cases.json |
while IFS=$'\t' read -r evidence_file; do
  digest="$(git -C "$case_checkout" cat-file blob \
    "${case_sha}:${evidence_file}" | shasum -a 256 | awk '{print $1}')"
  printf '%s  %s\n' "$digest" "$evidence_file"
done | LC_ALL=C sort > evidence.sha256
```

On systems with GNU coreutils, `sha256sum` can replace `shasum -a 256`. Store
`evidence.sha256`, the case ID, the full commit SHA, and tool versions with the
raw run record. The checksum file is an evaluation artifact, not a file to add
to this metadata directory.

## Evaluation boundary

- Give the agent only the prompt and allowlisted files for the selected case.
- Do not expose the upstream architecture image or other unlisted repository
  files merely because they exist at the commit.
- Treat documentation and configuration as evidence, not as answers to
  deployment choices that remain conditional or environment-specific.
- Preserve questions, approval, generated bundles, failures, and checksum
  provenance before making any comparative claim.
- Do not describe these cases as tested until their raw reviewed run records
  are published.
