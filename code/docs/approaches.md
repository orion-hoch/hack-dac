# Candidate pipeline approaches

Status: **not decided**. To be discussed once the local scorer runs, then dry-run on the Spark.

All approaches share two front stages:
1. Build an identity dictionary from paths and structured fields across the whole corpus, with identity
   resolution (full name ↔ username ↔ initials, accent variants), per repo first and then globally.
2. Make a deterministic pseudonym map: seeded hashing gives the same output every run. Pseudonyms are typed (person
   names, usernames, org names, domains `*.example`, part-number prefixes that keep the format).

The private map lives outside the submission.

## A. Format-aware rewriters (highest ceiling, most work)

- A handler per format family edits identity fields in place and re-serialises the container. The families: PDF
  (metadata + XMP + content-stream text + attachments), OLE property sets, the SW 2015+ container (re-deflate the
  sections, fix the section headers), zip containers (FCStd/3MF/F3D), STEP/IGES/SAT headers, DXF, SVG, images.
- Renames run through a single rename table, and references are rewritten from it.
- Risk: binary containers (SW 2015+, Creo, JT, DWG) need exact re-serialisation, or the file stops opening.

## B. Length-preserving byte substitution (fast, robust baseline)

- Replace every dictionary hit in raw bytes, in decompressed SW sections and in OLE streams with a pseudonym of
  **exactly the same byte length** (and same encoding: UTF-8 / UTF-16LE / latin-1), so offsets, counts and
  Hollerith prefixes stay valid.
- Rename paths with the same map.
- Weak spots: compressed streams (PDF Flate, zip deflate) need decompress → substitute → recompress. Pseudonym
  quality is constrained by length.

## C. Hybrid (likely best under the time limit)

- B everywhere as the safety net.
- A-style handlers for the highest-volume layers: paths, PDF metadata, PDF visible text, SW custom properties,
  Inventor property sets, STEP/IGES headers, FCStd/3MF XML, archives (recursive repack).
- Local OCR plus inpainting for title blocks and logos in raster images and PDF images, if time allows.

## D. Quick banked submission

Paths + PDF metadata + STEP/IGES headers + archive recursion only. Gets a real score early to calibrate the local
scorer against the real category counts.

## Things every approach must get right

- Never change extensions or restructure folders. Only replace the identifying spans of path segments.
- Identical input files → identical output files.
- Keep empty and invalid files (copied through, renamed if needed) and list them in README.txt.
- Archives: unpack, process recursively, repack in the same format with the same member order.
