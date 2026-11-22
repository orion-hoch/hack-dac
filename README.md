# hack-dac

De-identification pipeline for a large engineering CAD corpus (Builders Buildathon, Machina CAD De-ID track).
It replaces identifying names, accounts, contact details and logos with consistent same-length stand-ins, while keeping
every file usable: geometry, dimensions, references between drawings, parts and assemblies, and native formats.

## How it works

1. **One name mapping.** Every identity gets a stand-in of the same length and character classes (letters stay
   letters with their case, digits stay digits), derived from a secret key. Because nothing changes length, most
   edits are overwrites in place. The key and the identity table are private inputs and are not in this repository.
2. **Format-aware handlers.** Each file goes to a handler that edits only text fields:
   - text CAD formats (STEP, IGES, SAT, DXF, SVG, OBJ, WRL, Parasolid text): string slots and comments only,
     never numbers;
   - SolidWorks, Inventor, Solid Edge, NX, JT: compressed blocks are unpacked, edited at the same length and
     repacked to their original size, so nothing inside the file moves;
   - Creo: sections rewritten with both table-of-contents checksums recomputed;
   - DWG: in-place patch with checksum repair, accepted only if an independent reader sees text-only changes;
   - PDF: metadata rewrite and page-text replacement with permissively licensed libraries;
   - archives (zip, tar, gzip, .Z, 7z, RAR and zip-based CAD containers): members processed recursively and
     repacked.
3. **Path plan.** Only identifying words in folder and file names are replaced; everything else, including the
   extension, stays, so files can still be found.
4. **Gates.** Every output is re-opened by a reader that can refuse it, and numeric content of text CAD files must be
   identical before and after. A file that fails any gate is written out unchanged and listed.

## Layout

- `code/` the pipeline: `run_v2.py` (runner), `pipeline/` (routing, matcher, stand-in function, gates),
  `deid/` (handlers), `deid_eval/` (a local scorer used for development), `tools/`.
- `vm/` scripts to run on a fresh Ubuntu x86_64 machine: `setup.sh`, `fetch.sh`, `parity_check.sh`, `run.sh`,
  `upload.sh`.
- `submit.py` builds the submission folder and runs the preflight.

## Running

```bash
bash vm/setup.sh                 # Python 3.12, pinned requirements, helper tools built in user space
SOURCE=mirror bash vm/fetch.sh   # download the corpus and check every file size
bash vm/parity_check.sh          # refuse to continue unless this machine reproduces the reference outputs
ATTEMPT=attempt-N WORKERS=28 bash vm/run.sh
ATTEMPT=attempt-N bash vm/upload.sh
```

Private inputs (secret key, identity table, parity reference files, upload token) are copied to the machine
separately. Nothing derived from the raw corpus that still carries a real identifier belongs in this repository.

## Limits

Same-length stand-ins defeat search, not a determined reader: the shape of a public repository name can identify it.
Nothing here has been opened in the vendors' own CAD programs; validity is checked with independent readers and
container audits. Logos drawn as vector shapes, names that appear in no table, and encrypted archives are known gaps.

## Licence

MIT. See `LICENSE`.
