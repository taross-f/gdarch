# garch

A CLI tool to archive Google Drive folders and replace them with compressed archives.

## Features

- Recursively downloads all files from a specified Google Drive folder
- Creates a high-compression tar.xz archive
- Uploads the archive to the parent folder
- Optionally deletes the original folder

## Installation

### From PyPI
```bash
pip install garch
```

### From Source
```bash
# Install Poetry (if not already installed)
curl -sSL https://install.python-poetry.org | python3 -

# Clone and install
git clone https://github.com/yourusername/garch.git
cd garch
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
garch --folder-id <TARGET_FOLDER_ID> --credentials credentials.json

# When installed from source (using Poetry)
poetry run garch --folder-id <TARGET_FOLDER_ID> --credentials credentials.json

# Archive and delete the original folder
garch --folder-id <TARGET_FOLDER_ID> --credentials credentials.json --delete-folder

# Specify a custom archive name
garch --folder-id <TARGET_FOLDER_ID> --archive-name my_archive.tar.xz --credentials credentials.json
```

### Options

- `--folder-id`: Google Drive folder ID to archive (required)
- `--credentials`: Path to OAuth2 credentials file (defaults to credentials.json)
- `--archive-name`: Name for the uploaded archive file (optional)
- `--delete-folder`: Delete the original folder after archiving (flag)

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
```

## How It Works

1. Authenticates with Google Drive using OAuth2
2. Recursively lists all files in the specified folder
3. Downloads files while streaming them directly into a tar.xz archive
4. Uploads the compressed archive to the parent folder
5. Optionally deletes the original folder
6. Cleans up temporary files

## License

MIT License
