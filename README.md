# MIXIE

MIXIE is a local-first music ingestion and library management tool for Linux. It identifies recordings, checks album editions, prepares track metadata and artwork, imports verified files into a music library, and keeps independently verifiable archives of the original downloads and finished albums.

The application is written in Python. `mixie.py` is the command-line entry point; implementation modules are in `src/` and tests are in `tests/`.

## Requirements

- Python 3 with the dependencies in `.requirements.txt`
- FFmpeg for audio decoding and validation
- Chromaprint `fpcalc` in `.tools/` for fingerprint extraction
- An AcoustID client key for online identification
- Network access to AcoustID, MusicBrainz and the Cover Art Archive when using automatic identification

## Installation

From the `Scrubber` directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r .requirements.txt
```

Install FFmpeg separately and place a compatible `fpcalc` executable in `.tools/`. Configure your AcoustID client key using the `ACOUSTID_CLIENT_KEY` environment variable or `~/.config/mixie/acoustid_key` (a regular file with permissions `0600`). Do not commit API keys or credentials.

## Running MIXIE

```sh
.venv/bin/python mixie.py
```

The current terminal menu offers:

1. **Ingest Music** — stage Inbox deliveries, identify albums, import verified working copies, and finalize their required archives.
2. **Repair Library** — audit existing library metadata.
3. **Sync Device** — reserved for a future implementation.
4. **Review Pending Albums** — inspect unresolved matches and saved decisions.
5. **Backup Music Library** — optional whole-library ZIP backup, separate from the required per-album archives.
6. **Exit**.

Copy incoming ZIP files, FLACs, MP3s or supported album directories into `Inbox/`. MIXIE preserves unchanged originals until archive-backed cleanup is authorized by the ingestion workflow. It never treats a proposed tag correction as an edit to a downloaded source file.

The main interactive **Ingest Music** action enables cleanup for fully accounted physical deliveries **only after** the import, original-delivery archive, finished-album archive and recovery journal pass verification. An unresolved album within a shared delivery prevents deletion of that delivery; the completed albums can still be archived independently. Do not use your only copy of irreplaceable music as a test input.

## Directory layout

```text
MIXIE-Lab/
├── Music Library/             # Tagged music and album artwork
└── Scrubber/                  # Git repository root
    ├── mixie.py               # Application entry point
    ├── src/                   # Implementation
    ├── tests/                 # Isolated regressions
    ├── docs/                  # Architecture and development notes
    ├── Inbox/                 # Incoming deliveries
    ├── .staging/              # Source-bound working inventory
    ├── .staging-provenance/   # Staging and delivery records
    ├── .backups/
    │   ├── Originals/         # Verified source-delivery archives
    │   └── Library/           # Verified imported-album ZIPs
    └── .venv/                 # Local Python environment
```

Hidden receipts, journals, staging provenance and review records are operational data, not disposable caches. Do not relocate or delete them manually. The default library location is a sibling of `Scrubber` and the default Inbox is under `Scrubber`; configurable locations are planned, but are not yet implemented.

## Identification and review

MIXIE compares audio fingerprints and durations against recording and release data. Album-wide evidence helps resolve ambiguous recording candidates; conflicting versions or incomplete evidence are queued for review instead of being silently accepted. The review workflow can be postponed and resumed.

Imported files are written from independent working copies. Release-backed metadata and numbering are verified before commit. Missing genre information is not fabricated. Album completion, archive verification and source cleanup are separate checks.

For read-only inspection of albums currently queued for review:

```sh
.venv/bin/python mixie.py --check-pending
```

A separate `--retry-pending` command can retry supported queued albums. Archive-only retries are available from Review Manager or via `--finalize-verified`; this does not require repeating a successful identification.

## Testing

The test runners create isolated workspaces and use synthetic inputs. They should not be replaced with test discovery against the live library.

```sh
# Focused offline regression suite
.venv/bin/python -I -B tests/run_quick.py

# Complete isolated regression suite
.venv/bin/python -I -B tests/run_isolated.py
```

Additional instructions are in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). The archive and recovery rules are documented in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Safety and limitations

- Verify the Originals and Library archives before removing a source delivery.
- Keep a full original delivery if any of its albums remains unresolved.
- Do not delete import receipts, archive journals or provenance files to bypass a recovery check.
- A successful metadata match does not guarantee that artwork or archival finalization will succeed.
- Device synchronization, configurable library paths and the planned interactive Textual interface are not yet implemented.

MIXIE is under active development. For large or valuable collections, retain an independent backup outside its working directories.
