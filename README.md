# Music Scrubber

A Python script for organizing music from an Inbox into a music library while maintaining ZIP archives for each artist.

## Features

- Processes `.zip`, `.mp3`, and `.flac` files from the Inbox.
- Organizes supported tracks in Music Library by artist and album.
- Adds tracks, cover art, and other files from ZIPs to an appendable ZIP archive for each artist.
- Keeps extra files in the artist archive under `Extras/`; they are not copied to Music Library.
- Keeps multi-disc tracks in separate `Disc 1/`, `Disc 2/`, etc. folders in the artist archive.
- Checks ZIP archives for unsafe paths, excessive contents, and damaged files before extracting them.
- Stops with a clear error if a track is missing its required artist, album, or title tag.
- Skips duplicate paths when adding files to an existing artist archive.

The artist archives are organized copies of the music files and extras. They are not byte-for-byte copies of the original download ZIPs.

## Requirements

- Python 3
- Mutagen, listed in `requirements.txt`

## Installation

Run these commands from the `Scrubber` folder:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r .requirements.txt
.venv/bin/python scrubber.py
```

## Folder layout

The script creates its working folders if they do not exist:

```text
Music/
├── Music Library/
└── Scrubber/
    ├── scrubber.py
    ├── requirements.txt
    ├── Inbox/
    ├── .backups/
    └── .venv/
```

`Inbox` and `.backups` are beside the script. `Music Library` is one folder above `Scrubber`.

## Usage

1. Put ZIP, MP3, or FLAC files in `Inbox`.
2. Run the script from the `Scrubber` folder:

   ```bash
   .venv/bin/python scrubber.py
   ```

3. Supported tracks and `.jpg` or `.png` cover art are copied into Music Library.
4. The artist archives are saved in `.backups`, named `Artist.zip`.

Other files inside a ZIP are added to the artist archive under the album’s `Extras/` folder and are not copied to Music Library. Unsupported files placed directly in Inbox are left there.

## Source file cleanup

The script removes an Inbox file after successful processing only if the `os.remove(file_path)` line in the Inbox-cleanup section is enabled. If that line is commented out, the source remains in Inbox.

## Git

The `.gitignore` file excludes Inbox contents, `.backups`, `.venv`, and Python cache files. Keep music files and ZIP archives out of the repository.
