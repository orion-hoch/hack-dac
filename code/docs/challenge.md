# Challenge spec

Sources: the track page (authoritative) and `challenge.pdf`. Where they differ on attempts, input source or
submission, the page wins. The PDF is an older candidate version (3 attempts, AWS-only source and upload).

## Task

De-identify the corpus with consistent pseudonyms while keeping every file usable.

**Replace:** identifying company and person names, accounts, contact details, domains, addresses, logos, and
identifying project or part names and numbers.
- One original identity gets one pseudonym across the whole corpus. Distinct identities stay distinct.
- Fill identity fields with meaningful pseudonyms; don't blank them.

**Cover every layer:** visible text and images, file and folder names, PDF metadata and attachments, native CAD
properties, embedded previews, archive contents. "A plain text search or a clean preview is not sufficient."

**Preserve:** geometry, dimensions, tolerances, units, materials, configurations, BOMs, and links between drawings,
models and assemblies.
- Update references when you rename a file.
- Keep usable native files, not only exported PDFs or images.
- Leave format-required keys and non-identifying engineering data unchanged.

**Messy input:** duplicates and some invalid or empty files. Don't silently drop difficult files. Explain source
defects and omissions in README.txt using final paths or neutral descriptions. No original identifiers may remain
inside output archives.

**Reproducible:** "Process the full corpus with a reproducible pipeline on any machine. Keep temporary files and
private mappings out of your submission folder."

## Input

- 188,722 files, 139.7 GB. Mirror: `https://machina-input.joinbuilders.org` (no auth).
- `get-input.sh` (resumable, checks sizes; `--limit`, `--prefix`, `--jobs`, `--aria2`, `--md5`).
- `manifest.tsv`: path, bytes, md5 (blank for multipart uploads), url path.
- Needs a case-sensitive filesystem plus room for unpacked archives and output (~400 GB VM suggested).
- Original AWS source (PDF): `s3://machina-deid-v1-challenge/releases/v1/corpus/` (us-west-2).
- "Keep the raw input private. Do not publish or share it."

## Submission format

One location ending in `/` holding:

```
attempt-N/
  corpus/       de-identified files and archives (the only thing scored)
  code/         complete pipeline source + dependency versions or lockfiles
                (identifier-matching logic is allowed here)
  README.txt    exact run instructions, limitations, exceptions, source defects
```

Exclude credentials, raw input, temporary files and private run logs.

**Where to upload** (two options):
- **Builders folder.** `gs://builders-machina-submissions-1120/teams/<team-id>/attempt-N/`. Upload with `upload.py`
  (Python 3, no installs) using the token from "Get upload token" on the track page. Tokens last 1 hour; re-running
  skips files already uploaded. Files are deleted 10 days after upload. Leave a submitted attempt unchanged until
  results are published.
- **Own S3 bucket** in us-west-2, using the public-read bucket policy from challenge.pdf. Submit the folder that
  holds corpus/, not corpus/ itself.

## Scoring

- Each attempt is scored against **1,000 private checks** on `corpus/`, described as "names replaced consistently
  everywhere they appear, and the engineering files still usable".
- We see the total and pass counts by category, never which checks or files.
- **A check on a file the scorer cannot find or open fails (and shows as not checked).** "Keep each file's
  extension and the parts of its path that do not identify anyone so the scorer can find the files it checks."
- Baseline (input copied unchanged): **292**. Scorer version: `machina-deid-1`.
- Ties: the earlier attempt ranks first.
- Grading usually takes under 20 minutes (status page showed runs of 16-18 min), at most 3 hours.
- Organizers may re-grade everyone with a stricter grader during the event; the latest scores count.

## Limits and rules

- 20 scored attempts per team, 2 submissions per hour, one in flight at a time.
- A rejected submission still uses up the hourly slot. An attempt counts once queued unless rejected.
- Rejected without using an attempt: malformed location, corpus/ missing or empty, bucket outside us-west-2 or not
  public.
- corpus/ may hold at most **400,000 objects**. The scorer also rejects if finding the checked files takes more than
  **12 GB of downloads or 1 hour of reading**.
- Any single file **over 2 GB** in corpus/ is skipped during grading.
- If the scorer fails on an attempt, it waits for the organizers.
- Use only your own team's folder. The leaderboard at the cutoff is final: each team's best scored attempt counts,
  and nothing new is graded at the cutoff.

## Prize

$5,000. Machina refers its top 3 teams for a first-round interview (Engineering Researcher roles), so the code in
`code/` may get read by humans.

## Leaderboard snapshots

| When (2026-11-21) | 1st | 2nd | 3rd | 4th | 5th | Teams |
|---|---|---|---|---|---|---|
| first read | 707 | 682 | 652 | 632 | 620 | 104 |
| a few hours later | 852 | 724 | 707 | 682 | 652 | 109 |
