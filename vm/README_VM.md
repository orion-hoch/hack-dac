# pipeline_v3 on a rented x86_64 machine (Ubuntu 22.04, about 30 cores)

Order. Each step refuses to run if the step before it did not pass.
1. `bash setup.sh`: apt build tools, Python 3.12.3 (uv), pinned requirements, libarchive, unrar, LibreDWG dwgread,
   7zz, and the verifier's C helper. Writes ~/deid/env.sh.
2. Copy the private files (PRIVATE_FILES.txt).
3. `SOURCE=mirror JOBS=96 bash fetch.sh`: the whole corpus from the bundled manifest.
   - SOURCE=s3 uses the original bucket; any other value is taken as a base URL.
   - It is resumable and ends with an exact size check of all 188,722 files.
4. `bash parity_check.sh`: passes only if both of these equal the Spark's:
   - the public test vectors, and the stand-ins under the real key;
   - the 50 reference files: same bytes out, same new names.
   It writes private/PARITY_OK, tied to this exact code, table and key.
5. `ATTEMPT=attempt-N WORKERS=28 bash run.sh`: the full run.
   - It checks sizes and parity, then runs with a memory guard and is resumable.
   - It then builds code/ and README.txt and runs the preflight; on success it writes PREFLIGHT_OK.
6. `ATTEMPT=attempt-N bash upload.sh`: the organisers' upload.py.
   - A person writes a fresh token to ~/deid/private/token.txt; upload.py waits for it when the old one expires.
   - Only after PREFLIGHT_OK exists.

Settings:
- WORKERS counts every worker, one of them reserved for files of 200 MB or more.
- DEID_SW=native switches SolidWorks 2015+ to the native handler. The default is the fast route, chosen by measurement
  (findings/integration.md).
- DEID_NX_MODE=unchanged leaves newer NX parts as they are.
