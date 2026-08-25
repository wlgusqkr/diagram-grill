# Evidence-gated demo storyboard

Target length: 20–30 seconds. Target format: silent-friendly GIF or video with
large captions. Nothing may be recorded until the source-run gate passes.

## Source-run gate

Select one retained pair from the prelaunch evaluation. `01-checkout` is a
candidate because it tells a compact end-to-end story, but it may be used only
if the actual records satisfy every item below:

- Baseline and Diagram Grill runs used the same frozen model, client, prompt,
  fixture snapshot, permissions, and runtime settings.
- A reviewer recorded a concrete baseline failure against the fixed oracle.
- The paired skill run produced a genuinely different reviewed outcome.
- Both raw transcripts, tool logs, run records, and artifacts are retained.
- The comparison does not depend on a rerun, hidden hint, changed operator
  answer, or hand-edited output.
- Every on-screen claim maps to a field or reviewer note in the retained run
  records.

If no pair passes this gate, do not manufacture a Before/After demo. Postpone
the demo and the launch asset.

## Storyboard

| Time | Screen | Caption | Evidence requirement |
| --- | --- | --- | --- |
| 0–2s | Project name over the final diagram, briefly blurred or zoomed out | `Architecture diagrams should ask before they assume.` | No performance claim |
| 2–7s | Actual baseline prompt and output from the retained run | `[ONE REVIEWED BASELINE FAILURE]` | Exact reviewer record and raw transcript |
| 7–11s | Explicit Diagram Grill invocation and evidence citations appearing | `Inspect evidence first.` | Actual skill transcript and cited fixture lines |
| 11–16s | Real unresolved question, answer, and Architecture Brief | `Resolve decisions. Approve the brief.` | Unedited question, fixed operator answer, approval record |
| 16–21s | Validator/build command and bundle files appearing | `Validate, then render.` | Actual stdout, exit status, and retained bundle |
| 21–26s | Baseline and skill artifacts side by side with one factual highlight | `[REVIEWED DIFFERENCE]` | Paired artifacts and reviewer note |
| 26–30s | Public install command and repository URL | `Open source. Inspect the evidence.` | Signed-out install test against exact release |

Shorten the middle shots if the final asset is 20 seconds. Do not remove the
baseline evidence, approval moment, or final evidence cue.

## Editing rules

- Screen-record the retained run artifacts; do not retype a cleaner transcript.
- Time compression and jump cuts are allowed only when visually marked with a
  cut or `timelapse` label.
- Do not rearrange events or imply that asynchronous steps were instantaneous.
- Use one highlighted defect or difference, not a stack of unsupported claims.
- Keep commands, paths, and captions legible on a laptop-sized viewport.
- Remove local usernames, temporary paths, tokens, and unrelated notifications.
- Use captions so the demo works without audio. Do not rely on copyrighted
  music.
- Show client branding only for a client cleared by the compatibility gate.

## Claim-safe caption examples

Use only the example that exactly matches the reviewed record:

- `An unresolved ownership decision was omitted.`
- `A required asynchronous connection was missing.`
- `The output assumed a deployment boundary not present in the evidence.`
- `The approved brief preserved the recorded decision.`
- `The bundle contains JSON, generated Python, SVG, and a render manifest.`

Do not use `better`, `more accurate`, `hallucination-free`, or a percentage in
the demo unless the complete public evaluation directly supports that wording.

## Export checklist

- 20–30 seconds at normal playback.
- Text remains readable after GitHub and X compression.
- First frame works as a static poster.
- Final frame holds long enough to read the install command.
- Captions match reviewed evidence word for word.
- GIF/video and source recording hashes are retained.
- The README asset uses the same immutable demo that was reviewed.
