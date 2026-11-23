# Architecture

MIXIE separates ingestion, identification, tagging, storage and interactive review. The core modules live in `src/`, with `mixie.py` at the repository root as the launcher.

## Modules

| Module | Responsibility |
| --- | --- |
| `sort.py` | Inbox validation, source staging and provenance |
| `audio.py` | Decode checks and acoustic fingerprint collection |
| `lookup.py` | MusicBrainz/AcoustID lookup and release comparison |
| `tag.py` | Metadata audits, corrections and output-tag verification |
| `mixie_artwork.py` | Album artwork selection and validation |
| `mixie_batch_duplicates.py` | Repeated-delivery grouping and representative selection |
| `mixie_library_merge.py` | Safe placement of finished library files |
| `mixie_shared_originals.py` | Shared, content-addressed original-delivery archives |
| `mixie_finalizer.py` | Required archive verification and source retirement |
| `mixie_review_ui.py` and `review.py` | Saved decisions and manual-review workflow |
| `mixie_terminal.py` | Console formatting and operation progress |
| `mixie.py` | End-to-end orchestration and menu commands |

## Data flow

1. Validate and stage each physical delivery, recording provenance and inventory.
2. Fingerprint audio once and gather recording candidates.
3. Evaluate candidate albums using a one-to-one track mapping and recording-version evidence.
4. Produce metadata and artwork corrections on isolated working copies.
5. Verify the output and commit it into Music Library without overwriting conflicting files.
6. Store complete original downloads and finished album ZIPs with durable checksums and receipts.
7. Retire complete physical deliveries only after all required original, library and journal checks pass.

Unresolved albums remain available for Manual Review. A shared physical delivery cannot be removed while any of its constituent albums remains unresolved. Import completion and archive completion are tracked separately.

## Recovery invariants

- Source files remain unchanged until the finalizer has independently validated its deletion preconditions.
- A receipt is evidence to verify, not permission to delete or to override a file conflict.
- A verified album may wait for archive recovery without being sent back to music-metadata review.
- Current review tasks reflect fresh staged-source evidence; historical decisions are retained separately.
- Interrupted source retirement uses a durable journal, preflight checks and recoverable checkpoints.
- The full original-delivery archive is preserved even when several albums are imported separately.

## Runtime files

The hidden JSON ledgers, delivery manifests and recovery journals under `Scrubber` are excluded from Git. They must not be removed or relocated by source-tree maintenance. `docs/stress_test.txt` is a retained test fixture, not a command to run against live music.
