#!/usr/bin/env python3
"""
CLI Tool to Archive a Google Drive Folder and Replace It with the Archive

This script recursively downloads all files under a specified Google Drive folder,
creates a high-compression tar.xz archive, uploads the archive to the parent folder,
and optionally deletes the original folder.

Usage Examples:
  # Archive and upload without deleting the original folder:
  garch --folder-id <TARGET_FOLDER_ID> --credentials credentials.json

  # Archive, upload, and delete the original folder:
  garch --folder-id <TARGET_FOLDER_ID> --credentials credentials.json --delete-folder

  # Specify a custom archive filename:
  garch --folder-id <TARGET_FOLDER_ID> --archive-name my_archive.tar.xz --credentials credentials.json
"""

import argparse
import io
import lzma
import os
import posixpath
import shutil
import sys
import tarfile
import tempfile

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Google Drive scope with read and write permissions
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Smallest sensible LZMA2 dictionary size (1 MiB).
MIN_DICT_SIZE = 1 << 20
# Default upper bound for the LZMA2 dictionary size (256 MiB).
# A larger dictionary lets LZMA find matches across the whole solid tar stream
# (i.e. between different files), which improves the compression ratio for big
# folders. Memory usage while compressing is roughly 10.5x the dictionary size,
# so 256 MiB needs ~2.7 GiB of RAM. Raise it with --max-dict-size-mib for an
# even higher ratio if you have the memory to spare.
DEFAULT_MAX_DICT_SIZE = 256 << 20


def build_lzma_filters(total_size, max_dict_size=DEFAULT_MAX_DICT_SIZE):
    """
    Build an LZMA2 filter chain tuned for the best possible compression ratio.

    Uses preset 9 with the EXTREME flag (maximum effort, nice_len=273) and grows
    the dictionary to the smallest power of two that covers the entire archive,
    capped at ``max_dict_size``. Sizing the dictionary to the data is what lets
    the codec exploit redundancy across files in the solid tar stream.
    """
    dict_size = MIN_DICT_SIZE
    while dict_size < total_size and dict_size < max_dict_size:
        dict_size <<= 1
    # Doubling can overshoot a non-power-of-two cap, so clamp back down.
    dict_size = min(dict_size, max_dict_size)
    return [
        {
            "id": lzma.FILTER_LZMA2,
            "preset": 9 | lzma.PRESET_EXTREME,
            "dict_size": dict_size,
        }
    ]


def get_credentials(creds_file=None, token_file="token.json", token_json=None):
    """
    Retrieve OAuth2 credentials. Uses one of the following methods:
    1. Direct token JSON
    2. Existing token file
    3. OAuth2 credentials file
    """
    creds = None

    # 1. Try direct token JSON
    if token_json:
        try:
            import json

            token_data = json.loads(token_json)
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        except Exception as e:
            print("Failed to parse token JSON:", e)

    # 2. Try token file
    if not creds and os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    # 3. Try credentials file
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print("Failed to refresh credentials:", e)
                creds = None
        if not creds and creds_file:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
            with open(token_file, "w") as token:
                token.write(creds.to_json())
    return creds


def get_drive_service(creds):
    """Create a Google Drive API service instance."""
    return build("drive", "v3", credentials=creds)


def list_files(service, folder_id, parent_path=""):
    """
    Recursively list all files under the specified folder_id.
    Returns a list of dictionaries with keys: id, name, mimeType, size, relative_path.
    Files without size information (e.g. Google Docs) are skipped.
    """
    results = []
    page_token = None
    query = "'{}' in parents".format(folder_id)
    while True:
        response = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, size)",
                pageToken=page_token,
                pageSize=1000,
            )
            .execute()
        )

        for f in response.get("files", []):
            file_path = posixpath.join(parent_path, f["name"])
            if f["mimeType"] == "application/vnd.google-apps.folder":
                # Recursively process subfolders
                results.extend(list_files(service, f["id"], file_path))
            else:
                if "size" in f:
                    f["relative_path"] = file_path
                    results.append(f)
                else:
                    print(
                        "Skipping file (no size info):",
                        file_path,
                        "mimeType:",
                        f["mimeType"],
                    )
        page_token = response.get("nextPageToken", None)
        if not page_token:
            break
    return results


class LimitedStream:
    """
    A wrapper for a stream that limits the number of bytes read.
    This ensures that tarfile.addfile() reads the correct amount of data.
    """

    def __init__(self, stream, limit):
        self.stream = stream
        self.remaining = limit

    def read(self, size=-1):
        if self.remaining <= 0:
            return b""
        if size < 0 or size > self.remaining:
            size = self.remaining
        data = self.stream.read(size)
        self.remaining -= len(data)
        return data

    def readable(self):
        return True


def create_archive(service, creds, folder_id, archive_path, max_dict_size=DEFAULT_MAX_DICT_SIZE):
    """
    Download files under the specified folder_id and create a highly compressed LZMA archive at archive_path.
    Uses maximum compression settings for best compression ratio.
    """
    print("Retrieving file list from the specified folder...")
    files = list_files(service, folder_id)
    print("Total files to archive:", len(files))
    if not files:
        print("No files found in the specified folder.")
        return False

    total_size = sum(int(f["size"]) for f in files)
    processed_size = 0

    # Group similar files together (by extension, then path) so that the solid
    # LZMA stream can exploit redundancy between related files for a better ratio.
    files.sort(
        key=lambda f: (os.path.splitext(f["relative_path"])[1].lower(), f["relative_path"].lower())
    )

    filters = build_lzma_filters(total_size, max_dict_size=max_dict_size)
    print(f"Using LZMA dictionary size: {filters[0]['dict_size'] >> 20} MiB")

    try:
        # Open tar.xz with a custom LZMA2 filter chain for maximum compression.
        # tarfile.open does not accept custom filters, so wrap an LZMAFile and
        # stream the tar into it.
        xz_stream = lzma.LZMAFile(archive_path, mode="wb", format=lzma.FORMAT_XZ, filters=filters)
        tar = tarfile.open(fileobj=xz_stream, mode="w|", format=tarfile.GNU_FORMAT)
    except Exception as e:
        print("Failed to create archive file:", e)
        return False

    # Close the tar before the xz stream (reverse open order) so the archive is
    # finalized and flushed even if an error escapes the loop.
    with xz_stream, tar:
        for f in files:
            rel_path = f["relative_path"]
            file_id = f["id"]
            try:
                file_size = int(f["size"])
            except Exception as e:
                print("Invalid size info for file, skipping:", rel_path)
                continue

            print(
                f"Adding to archive: {rel_path} ({file_size} bytes) - {processed_size * 100 / total_size:.1f}% complete"
            )
            url = "https://www.googleapis.com/drive/v3/files/{}?alt=media".format(file_id)
            headers = {"Authorization": "Bearer " + creds.token}
            try:
                # Download file in streaming mode
                response = requests.get(url, headers=headers, stream=True)
                if response.status_code != 200:
                    print(
                        "  [ERROR] Failed to download file. HTTP status code:",
                        response.status_code,
                    )
                    continue
                response.raw.decode_content = True
                limited_stream = LimitedStream(response.raw, file_size)
                tarinfo = tarfile.TarInfo(name=rel_path)
                tarinfo.size = file_size
                tar.addfile(tarinfo, fileobj=limited_stream)
                processed_size += file_size
            except Exception as e:
                print("  [ERROR] Error while adding file to archive:", e)
                continue

    return True


def upload_file(service, local_file, name, parent_id):
    """
    Upload the local file to Google Drive under the specified parent folder.
    """
    file_metadata = {"name": name, "parents": [parent_id]}
    media = MediaFileUpload(local_file, mimetype="application/x-xz", resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    return file.get("id")


def delete_file_or_folder(service, file_id):
    """Delete the specified file or folder from Google Drive."""
    try:
        service.files().delete(fileId=file_id).execute()
        print("Successfully deleted. ID:", file_id)
    except Exception as e:
        print("Error deleting file/folder:", e)


def get_file_metadata(service, file_id):
    """Retrieve metadata (id, name, parents) for the specified file."""
    return service.files().get(fileId=file_id, fields="id,name,parents").execute()


def main():
    parser = argparse.ArgumentParser(
        description="Archive a specified Google Drive folder and replace it with the archive."
    )
    parser.add_argument("--folder-id", required=True, help="Google Drive ID of the target folder")
    parser.add_argument(
        "--credentials",
        help="OAuth2 credentials file (e.g., credentials.json)",
    )
    parser.add_argument(
        "--token",
        help="OAuth2 token JSON string (alternative to credentials file)",
    )
    parser.add_argument(
        "--archive-name",
        help="Name for the uploaded archive file (e.g., folder_archive.tar.xz). "
        "Defaults to folder name + '.tar.xz'",
    )
    parser.add_argument(
        "--delete-folder",
        action="store_true",
        help="Delete the original folder after archiving",
    )
    parser.add_argument(
        "--max-dict-size-mib",
        type=int,
        default=DEFAULT_MAX_DICT_SIZE >> 20,
        help="Maximum LZMA dictionary size in MiB (default: %(default)s). Larger "
        "values improve the compression ratio for big folders but use more "
        "memory (~10.5x the dictionary size while compressing).",
    )
    args = parser.parse_args()

    if args.max_dict_size_mib < 1:
        print("Error: --max-dict-size-mib must be at least 1")
        sys.exit(1)

    if not args.credentials and not args.token:
        print("Error: Either --credentials or --token must be specified")
        sys.exit(1)

    # Initialize credentials and Drive API service
    creds = get_credentials(creds_file=args.credentials, token_json=args.token)
    if not creds:
        print("Failed to obtain valid credentials")
        sys.exit(1)

    service = get_drive_service(creds)

    # Retrieve metadata for the target folder (name, parent folder, etc.)
    folder_meta = get_file_metadata(service, args.folder_id)
    folder_name = folder_meta.get("name", "folder")
    parent_ids = folder_meta.get("parents", [])
    if not parent_ids:
        print("No parent folder found. Cannot process root-level folders.")
        sys.exit(1)
    parent_id = parent_ids[0]

    archive_name = args.archive_name if args.archive_name else f"{folder_name}.tar.xz"
    print("Archive file name:", archive_name)

    # Create archive in a temporary directory
    temp_dir = tempfile.mkdtemp()
    archive_path = os.path.join(temp_dir, archive_name)
    print("Creating archive at temporary location:", archive_path)

    if not create_archive(
        service,
        creds,
        args.folder_id,
        archive_path,
        max_dict_size=args.max_dict_size_mib << 20,
    ):
        print("Failed to create archive.")
        shutil.rmtree(temp_dir)
        sys.exit(1)

    print("Archive created successfully. Starting upload...")
    archive_file_id = upload_file(service, archive_path, archive_name, parent_id)
    print("Upload complete. Archive file ID:", archive_file_id)

    if args.delete_folder:
        print("Deleting original folder as specified...")
        delete_file_or_folder(service, args.folder_id)
    else:
        print("Original folder retained (option --delete-folder not specified).")

    # Clean up temporary directory
    shutil.rmtree(temp_dir)
    print("Operation completed successfully. Enjoy your productive day!")


if __name__ == "__main__":
    main()
