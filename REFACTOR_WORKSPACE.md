# PaddleOCR Hybrid Refactor Workspace

Created: 2026-09-05

This directory is an isolated source snapshot for the PaddleOCR hybrid recognition
refactor. All implementation and verification for this refactor happens here.

## Source snapshots

- `Lexoid/` was copied from `/Users/dongdong/code/lexiod/lexiod/Lexoid/`.
- `lexiod-pipeline/` was copied from `/Users/dongdong/code/lexiod/lexiod-pipeline/`.
- Existing modified and untracked source files were included.

## Deliberate exclusions

- Git metadata (`.git`)
- Python virtual environments (`.venv`)
- Python and pytest caches
- Model/test run caches and probe output
- Lexoid `output/`, `tmp/`, and `work/` generated artifacts
- Pipeline `.pipeline-logs/`, `.texopt-probe/`, and `files/mnt/`

The original directories remain unchanged and are not used for refactor edits.

## Operating constraints

- Never stage or commit files.
- Production target: CPU Docker on the current Apple M5 macOS host.
- GPU device strings remain supported by the recognition contract, but this refactor
  does not build or require a GPU runtime.
- Follow the design and implementation plan under `Lexoid/docs/superpowers/`.

## Current production run

### Batch-only scope (2026-09-06)

Current work is limited to batch records. The existing U1 partition plan now
contains only `Downloads/U1/批次数据/`: 457 PDFs, assigned to workers 01-04 as
113, 112, 115 and 117 files. Existing assignments and the 32 cached recognition
pages for worker 03's in-progress batch PDF were preserved. The partition plan
passed the production validator against the current source tree.

The U2 queue now contains only the four pending batch PDFs under
`批次/A37Z201202605029/`; the 52 previously accepted batch exports remain excluded.
The decrypted supplemental queue is empty and disabled because its three files
were all non-batch records. Do not start its former worker.

All six old paused containers were removed to discard their in-memory non-batch
queues. No conversions are running. Recreate only the four U1 workers and the U2
worker using the existing filtered queue paths and updated model env files when
resumption is requested. Batch artifacts, model weight volumes and credentials
were preserved. Sanitized old worker settings are recorded in
`../data/audits/2026-09-06/retired-worker-configs.json`.

Non-batch OOX/deviation TeX, intermediate artifacts and caches were deleted:
3,068 files, 1,006,444,407 bytes. All 558 retained batch/support files passed
before/after SHA-256 checks. The audit is
`../data/audits/2026-09-06/batch-only-cleanup.json`. Original PDFs in Downloads
and original PDF backups were preserved pending clarification of source deletion
scope. Earlier inventories and deployment counts below are historical.

The selected mode is pure vision (`RECOGNITION_OCR=none`). Model selection is
configured through the environment; see the role table below. Automatic page
orientation still uses the cached lightweight
Paddle orientation classifier. No container memory or swap limit is configured.

### Model configuration (2026-09-05)

| Variable | Purpose | Current model |
| --- | --- | --- |
| `LEXOID_MODEL` | Full-page PDF recognition and raw TeX | `gpt-6-astra` |
| `RECONCILE_MODEL` | Uncertain field crop review | `gpt-6-astra` |
| `TEXOPT_MODEL` | Cached per-table semantic naming | `gpt-5.6-terra` |
| `TEXOPT_REPAIR_MODEL` | TeX syntax and failed compilation repair | `gpt-5.6-sol` |
| `DEFAULT_LLM` | General LLM document parsing | `gpt-5.6-terra` |
| `LEXOID_SCHEMA_MODEL` | Schema-based extraction | `gpt-5.6-terra` |
| `FORMKIT_MODEL` | Form discovery and crop rereading | `gpt-6-astra` |
| `CODEX_MODEL` | Standalone Codex failed-file repair script | `gpt-5.6-sol` |

`Lexoid/.env` and `lexiod-pipeline/files/.env` contain Chinese purpose comments.
Their example files and the optional Streamlit `lexiod-pipeline/.env` agree with
these settings. Compose loads the pipeline env file second, so shared variables
must agree. Explicit CLI/API model arguments take precedence. Missing or blank
model settings fail before the relevant model is used; offline commands and help
do not require paid-model settings. `OPTIMIZER_EXTRA_ARGS` must not embed model
names that override the dedicated variables.

Optional local models use `DEFAULT_LOCAL_LM`, `LEXOID_CLIP_MODEL`, and
`PADDLE_LAYOUT_MODEL`. `PADDLE_PREFETCH_MODELS` is a JSON list for downloading the
installed PaddleOCR runtime's component weights, including VL; it does not enable
inference. The configured layout model is included automatically. Changing the
CLIP model requires regenerating its embedding caches.

The six paused workers were retired during the batch-only cleanup above. Recreate
the five remaining batch workers with the rebuilt image and updated env files
when resuming is authorized. Preserve their filtered queues, mounts, checkpoints
and exclusions; the non-batch supplemental decrypted queue is retired.
Changing a stage model changes its fingerprint, so completed affected stages may
be reconsidered; recognition remains reusable when only optimizer models change.

Verification: both `lexiod-refactor:local` and `lexiod-refactor-test:local` were
built successfully. The final image passed 282 offline tests, including current
environment resolution, explicit overrides, missing settings, independent syntax
repair routing, and offline CLI operation. Production env files, examples and the
optional UI env agree on model settings; the prefetch list resolves to 13 local
models. No paid model calls or production conversion were started for this change.

The page-checkpoint correction makes the CLI writer persist exactly one
`LEXOID_PAGE_COMPLETED` marker per page. Incorrect or duplicate incoming markers
remain errors. The subsequent physical-pagination correction also removes the
implicit title-page break, applies each recognition page's reading dimensions,
and checks the actual PDF before publishing. The final rebuilt image passed 255
offline tests on 2026-09-05, including real XeLaTeX pagination regressions.

Batch optimization passes `--page-layout-evidence` and uses the optimizer version
`texopt-layout-v2`. Existing recognition and reconciliation caches are reusable.
Both compilation passes must succeed; `layout_check` must also confirm the PDF
page count, each source page's start/end output pages, reading dimensions, and
absence of text outside the paper bounds. At most two spacing/font adjustments
are attempted for an offending page, then the file fails without publication.
This check does not validate graph completeness or all within-cell text overlaps.

The known U1 sample now produces 5 pages instead of 6, and the benchmark sample
produces 3 instead of 4. Offline comparisons preserved all 120 and 50 existing
field payloads, respectively. Evidence and previews are under
`../work/refactor/pagination-validation/`. The old U1 optimized result and its
reports were archived, with hashes checked, under
`../data/.archive/2026-09-05/pagination/U1/OOX/optimized/` before production resumed.
The corrected U1 result has passed production validation and was republished.

Each optimized report contains `layout_check`. Diagnostic PDFs and JSON are kept
beside the intermediate optimized TeX in each document's `.pipeline/` directory
as `*.optimized.layout.pdf` and `*.optimized.layout.json`.

## Final TeX tree

Final exports are collected in `../data/optimized/`, mirroring `../Downloads/`.
For example, `Downloads/U1/<category>/<batch>/record.pdf` becomes
`data/optimized/U1/<category>/<batch>/record.tex`. The original category and batch
names are retained. Only the PDF extension changes; no `.optimized` suffix is
added to the final filename. Directories for pending source PDFs may be empty.

`PIPELINE_PUBLISH_ROOT` controls this clean export root. Unit workers set it to
`/data/optimized/U1` or `/data/optimized/U2`. Recognition TeX, evidence, state,
caches and logs remain under `data/U1` and `data/U2`; reports are kept in the
document's `.pipeline/` directory and are not copied into the final TeX tree.
Changing only the publication root reuses completed pipeline stages.

Run the migration utility with workers stopped:

```sh
python -m texopt.publication --source-root /input \
  --work-root /data --publish-root /data/optimized \
  --audit /data/audits/2026-09-05/clean-tex-publication.json
```

It checks destination conflicts before moving files, verifies SHA-256 hashes,
preserves former sidecars under `.pipeline/<stem>/published-sidecars/`, and builds
the matching directory tree without creating placeholder TeX files.

The migration completed on 2026-09-05: 53 TeX files (1 U1 and 52 U2) and three
sidecars were moved, and all 56 destination hashes matched. The final tree
contains only TeX files and mirrors 94 source-parent directories for 1,041 PDFs.
All 52 accepted U2 results remain excluded from the 40-file run queue. The rebuilt
image passed 258 offline tests, and both unit workers resumed with the new export
roots. Migration details are in the audit file above.

## U1 partition workers

U1 is divided into four static, disjoint PDF queues, balanced by readable page
count. The plan is `../data/audits/2026-09-05/u1-partitions.json`. Each PDF is
assigned exactly once using its full path relative to `Downloads/U1`, including
category and batch directories. Unreadable PDFs remain assigned and are reported
as failures; they are not silently dropped from the source inventory.

The first worker keeps all previously started files and uses `data/U1`, retaining
its existing stage fingerprints, recognition cache and SQLite state. Workers
02-04 use `data/U1/.workers/02` through `04` for their raw TeX, state, caches and
logs. All four publish only final TeX into `data/optimized/U1`, with the original
relative paths. The per-work-root batch locks prevent duplicate containers from
running the same partition simultaneously.

Create the plan once, after stopping the old unpartitioned U1 worker:

```sh
docker compose run --rm --no-deps worker python -m texopt.partitions plan \
  --source-root /input/U1 --output-root /data/U1 \
  --publish-root /data/optimized/U1 --workers 4 \
  --plan /data/audits/2026-09-05/u1-partitions.json
```

Start the four containers from the existing plan. Run only the corresponding
iteration when restarting one stopped worker. Do not start the old unfiltered
U1 command alongside these workers or regenerate the plan while they are active.
The containers are removed on exit; all durable data remains local.

```sh
for partition in 01 02 03 04; do
  partition_work=/data/U1
  if [ "$partition" != 01 ]; then
    partition_work=/data/U1/.workers/$partition
  fi
  docker compose run -d --rm --no-deps --name lexiod-refactor-u1-$partition \
    -e PDF_SOURCE_ROOT=/input/U1 -e PIPELINE_OUTPUT_ROOT="$partition_work" \
    -e PIPELINE_PUBLISH_ROOT=/data/optimized/U1 \
    -e RECOGNITION_OCR=none -e PIPELINE_STAGE_TIMEOUT_SECONDS=21600 worker \
    python -m texopt.partitions run \
    --plan /data/audits/2026-09-05/u1-partitions.json --worker "$partition"
done
```

Each U1 worker retains four concurrent vision requests and two field-review
requests. No container memory or swap limit is configured. More workers increase
aggregate API demand, so speedup depends on provider capacity and rate limits.

Deployment on 2026-09-05 passed 266 offline tests, including simultaneous worker
execution, exact selection of same-named PDFs, checkpoint reuse and partition
coverage validation. The original U1 container was replaced by
`lexiod-refactor-u1-01` through `lexiod-refactor-u1-04`; U2 stayed running.

| Worker | PDFs | Readable Pages |
| --- | ---: | ---: |
| 01 | 129 | 5,522 |
| 02 | 122 | 5,523 |
| 03 | 125 | 5,522 |
| 04 | 125 | 5,522 |

All 501 source PDFs are assigned exactly once. The eight unreadable PDFs are
included in the file counts and contribute zero to page counts. All eight files
previously started by U1 stayed with worker 01. Its first completed PDF reused
all three stages; all 53 previously published U1/U2 TeX files retained their
original hashes. Worker 02 subsequently published its first new final TeX.
The initial runtime check found no cgroup OOM events in the four U1 containers.

## U2 worker

U2 uses `../data/audits/2026-09-05/u2-run-queue.json`: 36 missing conversions
and four replaced PDFs ending in 5560, 6000, 6040 and 6090. The other 52 existing
outputs were accepted by the user and are excluded. The four incorrect optimized
outputs are archived under `../data/.archive/2026-09-05/u2-orientation/`.
Use the queue when restarting U2:

```sh
docker compose run -d --rm --no-deps --name lexiod-refactor-u2 \
  -e PDF_SOURCE_ROOT=/input/U2 -e PIPELINE_OUTPUT_ROOT=/data/U2 \
  -e PIPELINE_PUBLISH_ROOT=/data/optimized/U2 \
  -e RECOGNITION_OCR=none -e PIPELINE_STAGE_TIMEOUT_SECONDS=21600 worker \
  python -c 'import json
import sys
from pathlib import Path
from texopt.stages import BatchConfig, run_batch
plan = json.loads(Path("/data/audits/2026-09-05/u2-run-queue.json").read_text())
sys.exit(run_batch(Path(plan["source_root"]), Path(plan["output_root"]), BatchConfig.from_env(), include=plan["include"]))'
```

Inspect progress with `docker exec lexiod-refactor-u1-01 texopt-pipeline --status`
(replace `01` with `02`, `03` or `04` for another U1 partition)
or `docker exec lexiod-refactor-u2 texopt-pipeline --status`. Each run makes one
pass through its source list. Failed documents are recorded for retry on a
subsequent run; successful stages and valid page drafts are reused.
