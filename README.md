# gdarch

[![CI](https://github.com/taross-f/gdarch/actions/workflows/ci.yml/badge.svg)](https://github.com/taross-f/gdarch/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/taross-f/gdarch/branch/main/graph/badge.svg)](https://codecov.io/gh/taross-f/gdarch)
[![PyPI Downloads](https://static.pepy.tech/badge/gdarch)](https://pepy.tech/projects/gdarch)

A CLI tool to archive Google Drive folders and replace them with compressed archives.

## Motivation

Google Drive storage space is often filled with large folders that are rarely accessed but need to be kept for reference or backup purposes. This tool helps you free up storage space by:

1. Automatically compressing such folders into high-compression archives
2. Replacing the original folders with their compressed versions
3. Maintaining the same folder structure and accessibility

This way, you can keep your important data while significantly reducing storage usage.

## Features

- Recursively downloads all files from a specified Google Drive folder
- Creates a high-compression tar.xz archive (LZMA2, preset 9 + EXTREME)
- Automatically sizes the LZMA dictionary to the archive — and to the memory
  actually free on your machine — so redundancy across files can be exploited
  for the best possible compression ratio
- Collapses byte-identical files into tar hard links using the checksums Drive
  already reports, so duplicates are neither downloaded nor stored
- Groups similar files together and pushes already-compressed formats (JPEG,
  MP4, ZIP, docx…) to the end of the stream, keeping compressible data inside
  one match window
- Tunes the LZMA context model to the payload (`pb=0` for text-dominated folders)
- Uploads the archive to the parent folder
- Optionally deletes the original folder

The archive is a plain `tar.xz`: `tar -xJf archive.tar.xz` restores everything,
hard links included, with no need for gdarch.

## Installation

### From PyPI
```bash
pip install gdarch
```

### From Source
```bash
# Install Poetry (if not already installed)
curl -sSL https://install.python-poetry.org | python3 -

# Clone and install
git clone https://github.com/taross-f/gdarch.git
cd gdarch
poetry install
```

## Usage

1. Get OAuth2 credentials from Google Cloud Console:
   - Visit [Google Cloud Console](https://console.cloud.google.com/)
   - Create or select a project
   - Go to APIs & Services > Credentials
   - Create an OAuth 2.0 Client ID
   - Download the credentials and save as `credentials.json`

2. Run the command:

```bash
# When installed from PyPI
gdarch --folder-id <TARGET_FOLDER_ID> --credentials credentials.json

# When installed from source (using Poetry)
poetry run gdarch --folder-id <TARGET_FOLDER_ID> --credentials credentials.json

# Archive and delete the original folder
gdarch --folder-id <TARGET_FOLDER_ID> --credentials credentials.json --delete-folder

# Specify a custom archive name
gdarch --folder-id <TARGET_FOLDER_ID> --archive-name my_archive.tar.xz --credentials credentials.json

# Pin the LZMA dictionary instead of sizing it from free memory
gdarch --folder-id <TARGET_FOLDER_ID> --credentials credentials.json --max-dict-size-mib 768

# Keep every duplicate as its own copy (no tar hard links)
gdarch --folder-id <TARGET_FOLDER_ID> --credentials credentials.json --no-dedup
```

### Options

- `--folder-id`: Google Drive folder ID to archive (required)
- `--credentials`: Path to OAuth2 credentials file (defaults to credentials.json)
- `--archive-name`: Name for the uploaded archive file (optional)
- `--delete-folder`: Delete the original folder after archiving (flag)
- `--max-dict-size-mib`: Maximum LZMA dictionary size in MiB, or `auto`
  (default). The dictionary is grown to cover the whole archive up to this cap,
  letting LZMA find matches *across* files in the solid stream. `auto` derives
  the cap from the memory that is free right now (64 MiB–1 GiB), since encoding
  costs roughly 10.5x the dictionary size in RAM; pass a number to pin it.
- `--no-dedup`: Store every duplicate file in full instead of emitting a tar
  hard link to the first copy. Only needed if your extraction tool cannot handle
  hard links.

### Finding Folder ID

The folder ID is the last part of the Google Drive folder URL:
```
https://drive.google.com/drive/folders/1234567890abcdef
                                      ^^^^^^^^^^^^^^^^
                                      This is your folder ID
```

## Development

```bash
# Install dependencies
poetry install

# Run tests
poetry run pytest

# Format code
poetry run black .
poetry run isort .

# Measure the compression strategy (offline, no credentials needed)
poetry run python -m bench.benchmark
```

See [`bench/README.md`](bench/README.md) for what the benchmark measures and how
to run it against your own folders.

## How It Works

1. Authenticates with Google Drive using OAuth2
2. Recursively lists all files in the specified folder, picking up the size and
   checksum Drive reports for each one
3. Orders the files so related data sits together and already-compressed formats
   go last, then picks LZMA2 parameters to match the payload
4. Downloads files while streaming them directly into a tar.xz archive, skipping
   the download entirely for content that is already in the archive and linking
   to it instead
5. Uploads the compressed archive to the parent folder
6. Optionally deletes the original folder
7. Cleans up temporary files

### Why those choices

`tar.xz` is a *solid* archive: every file is compressed against everything
before it. Two things decide how much of that redundancy LZMA can actually
reach — how far apart related bytes sit, and how wide the match window is.
Grouping similar files and exiling incompressible blobs to the end shortens the
distances; sizing the dictionary to free memory widens the window. What still
falls outside the window is caught by checksum deduplication, which removes
duplicate content outright rather than relying on the codec to match it.

## License

MIT License
