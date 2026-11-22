# Local scorer design (imitating the hidden `machina-deid-1` scorer)

Status: **implemented and validated on a 400-file subset** (`deid_eval/`, launcher `deid.py`).

```bash
P=.venv/bin/python   # deps: requirements.txt
$P -I deid.py truth  --input ~/machina/input --out ~/machina/private/truth --workers 14   # once, on the raw input
$P -I deid.py checks --truth ~/machina/private/truth --out ~/machina/private/checks.jsonl # fixed seed, 1,000 checks
$P -I deid.py score  --checks ~/machina/private/checks.jsonl --corpus <attempt>/corpus --report ~/machina/private/report.json
$P -I deid.py preflight --attempt <attempt> --input ~/machina/input
```

Validation so far (400 catalog files, 988 checks):
- **Unchanged copy:** exactly **292**, with every usability check passing and every name check failing. This matches
  the real baseline.
- **Path-only rename control** (`tools/control_path_rename.py`): 476. All `names.paths` checks pass, and renamed
  files are still found (0 not-checked). Content leaks still fail and usability is unchanged.
- **Speed:** about 20 files/s with 10 workers. A full truth build is a few hours of CPU on the shared Spark.
  Scoring takes about 40 s.

Everything under `~/machina/private/` contains real identifiers. Never commit it.

## What we know about the real scorer

- 1,000 private checks on corpus/, reported as a total plus pass counts per category. Described as "names replaced
  consistently everywhere they appear, and the engineering files still usable".
- It locates files by **extension plus the non-identifying parts of the path**. Missing or unopenable file means
  every check on it fails ("not checked").
- It may download up to 12 GB / read for up to 1 hour to find the files it checks. That suggests it lists corpus/,
  matches path skeletons, then downloads candidates to disambiguate.
- An unchanged copy scores 292. A copy passes usability checks and fails name checks, so we assume **~292 usability
  checks and ~708 name checks**.
- The real per-category totals (if the feedback shows `passed/total`) will let us replace these assumed quotas
  after our first submission.

## Pipeline of the harness

1. **`truth`** (run once on the full input, on the Spark):
   - Pass 1: parse every file and extract identity slots, engineering tokens, references, structure, geometry
     fingerprints and per-chunk hashes. Cache each file's text "bundle".
   - Build the identity dictionary.
   - Pass 2: scan all bundles and paths with Aho-Corasick for every identity, recording occurrences per file and
     category, and which chunks (OLE stream / SW section / zip member) are identity-free.
2. **`checks`**: sample 1,000 checks with a fixed seed and category quotas, stratified by format and source group.
   Each check record is self-contained (expected values embedded). It contains real identifiers, so it's private
   and never committed.
3. **`score`**: align output files to input files, evaluate checks, and print the total plus per-category
   pass / total / not-checked. Also write a private failure log listing exactly which checks failed. That log is
   the advantage over the real scorer.
4. **`preflight`**: submission-level rules.
   - corpus/ exists, is non-empty, and has ≤ 400k objects; flag files over 2 GB.
   - README.txt and code/ exist.
   - No mapping/temp/log files in corpus/.
   - Extensions unchanged; file count matches the input minus documented omissions.

## Identity dictionary (truth)

- **Paths:**
  - GitHub/GitLab owner handles (`<owner>__`) and repo names (distinctive ones only: length ≥ 6 or containing
    digits, case changes or hyphens).
  - Web hostnames.
  - A hand-made list of org/person/project names in the 64 catalog folder names and in `sources/existing`
    subpaths (kept local, not committed).
- **Structured slots:**
  - PDF Author / XMP creator.
  - OLE SummaryInfo author/lastauthor; DocSummary company/manager.
  - Inventor Design Tracking checked-by / approved-by / designer / engineer / authority (by FMTID+PID).
  - Solid Edge Username.
  - SW custom props: author, last-saved-by (including localised keys), drawn/checked/approved, company, comments.
    Also SW docProps creator / lastModifiedBy / Company.
  - FCStd CreatedBy / LastModifiedBy / Company. 3MF Designer / Copyright.
  - STEP FILE_NAME author/organisation. IGES global params 21/22 (author/org).
  - PostScript %%For / %%Creator user. DXF $LASTSAVEDBY. EXIF Artist / Copyright / XPAuthor.
- **Patterns anywhere:**
  - Usernames in `C:\Users\<u>`, `Documents and Settings\<u>`, `/home/<u>`, `/Users/<u>`, `\\host\users\<u>`.
  - UNC hosts, emails, non-allowlisted domains/URLs, phone/QQ/WeChat numbers.
  - "Designed by / Drawn by / © by:" values in drawing text.
- **Filters:**
  - Drop generic accounts (Administrator, Operator, User, TEMP...).
  - Drop software vendors and tools (SolidWorks, Dassault, Autodesk, PTC, Siemens, Spatial, ezdxf, netfabb,
    Inkscape, cairo...).
  - Drop placeholders (Author, Unknown, UNSPECIFIED, empty).
- **Normalization:** NFKD accent folding plus lowercase. Variants join multi-word names with ''/'_'/'-'/'.'.
- **Length rules:** identities shorter than 4 characters are only checked inside their own slot. 4-5 characters
  need word boundaries. 6+ match as substrings.

## Bundle categories (also the report categories)

| Category | Content |
|---|---|
| `names.paths` | the relative path (all folder and file names) |
| `names.pdf_metadata` | PDF Info, XMP, attachment names |
| `names.visible_text` | PDF page text, SVG `<text>/<title>/<desc>`, DXF TEXT/MTEXT/ATTRIB, PostScript show strings |
| `names.native_properties` | OLE property sets + stream strings, SW 2015+ decompressed sections, FCStd/3MF/F3D members |
| `names.cad_headers` | STEP header + quoted strings, IGES S/G + Hollerith strings, SAT/Parasolid headers, Creo strings, STL/OBJ/WRL/UNV names and comments, DWG/JT/CATIA strings |
| `names.archives` | members (names + recursive bundles) of .zip/.tar/.gz/.tgz; raw strings of .7z/.rar |
| `names.images` | EXIF / PNG text / XMP (+ optional OCR; off by default) |

## Check types and default quotas (sum = 1,000)

**Names group: 708**

| Category | Quota | Pass condition |
|---|---:|---|
| names.paths | 170 | No variant of the identity in the aligned output path |
| names.pdf_metadata | 55 | Identity absent from the whole output file bundle |
| names.visible_text | 65 | 〃 |
| names.native_properties | 140 | 〃 |
| names.cad_headers | 80 | 〃 |
| names.archives | 50 | 〃 |
| consistency | 90 | See below |
| meaningful | 58 | See below |

- **consistency** (70 same-identity checks + 20 distinct checks).
  - *Same:* for one identity with several slot occurrences across files and layers, capture the pseudonym at each.
    Turn the input slot value into a regex: identity spans become groups, the rest stays literal. Full-match it
    against the output slot (aligned path, or same field). Pass when every capture is known, all are equal, and none
    equals the original.
  - *Distinct:* two clearly different identities of the same type map to different pseudonyms.
- **meaningful:** an identity field (author, company, designer...) still exists in the output, is non-empty, is
  not the original, is not a placeholder (REDACTED / XXX / *** / ANON / Unknown / empty), and has ≥ 2 letters.

**Usability group: 292** (an unchanged copy passes all of these)

| Category | Quota | Pass condition |
|---|---:|---|
| usable.opens | 90 | Found and opens with a format validator; structure counts equal the input (PDF pages, STEP entities, zip members, OLE streams, SW sections, STL triangles, image dims...) |
| usable.geometry | 70 | Format geometry fingerprint unchanged (below) |
| usable.engineering | 60 | Sampled engineering token still present in the same field (units, material, mass props, tolerances, config names, STEP schema, DXF $INSUNITS, IGES units, PDF dimension and tolerance text) |
| usable.references | 35 | Drawing→model / assembly→part / CMNM / mtllib / SW-Material@ links: the output source file still references the basename of the aligned output target |
| usable.native_format | 20 | Same full extension (including Creo `.N`) and same detected kind |
| usable.duplicates | 17 | Two files with identical input bytes are byte-identical in the output |

If a category doesn't have enough candidates, its quota goes to the other categories in the same group, so the
group totals stay 708/292.

## Geometry fingerprints

- **STEP:** hash of the number sequence in DATA with quoted strings removed.
- **IGES:** P-section numbers with Hollerith strings removed.
- **STL:** binary payload after byte 84, or the ASCII numbers.
- **OBJ:** v/vt/vn/f lines. **SAT:** body numbers with `@n` strings removed. **WRL / PLY / OFF / UNV:** numeric content.
- **DXF:** coordinate group codes 10-59 and 210-239.
- **3MF:** vertex/triangle numbers. **FCStd:** hashes of the .brp members.
- **PDF:** page count, page sizes, vector-drawing and image counts on the first 3 pages.
- **Images:** 32×32 grayscale thumbnail, mean absolute difference < 3. Only images with no identity in them.
- **Natives (OLE, SW 2015+, zips):** the multiset of identity-free chunk hashes must be a subset of the output's
  chunk hashes.
- **Opaque binaries (Creo, JT, DWG, CATIA, Parasolid binary):** ≥ 99% of sampled identity-free lines still present.

## Alignment ("finding the files")

For each checked input file, build a regex from its relative path:
- identity spans become `([^/]*?)`;
- everything else is literal;
- the extension is matched case-insensitively.

Candidates are output files with the same depth and extension. Exact relative path wins; otherwise the regex
full-match wins; ties go to the closest size and same kind. No match means not found and the check fails.

## Calibration plan

1. Run `score` on an unchanged copy. It should come out ≈ 292 by construction. Verify it.
2. After the first real submission, map the real category pass counts onto ours and adjust quotas and pass rules.
3. Keep an independent **canary** test: plant fake identities in copies of files of every format and layer, run the
   pipeline, and verify the canaries are gone and the files still validate. This measures recall without relying on
   our own dictionary.

## Known blind spots

- Our truth only contains identities our extractor recognises. The real scorer surely has curated identities we'll
  miss, especially free-text names, logos in pixels, and project names. Treat our score as an upper bound on the
  names side.
- OCR and logo checks are off by default (slow, noisy).
