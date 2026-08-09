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

from gdarch.compression import (
    DEFAULT_MAX_DICT_SIZE,
    MIN_DICT_SIZE,
    auto_max_dict_size,
    build_lzma_filters,
    order_files,
    text_byte_ratio,
    write_files,
)

# Google Drive scope with read and write permissions
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Metadata requested for every file. The checksums ride along with the listing
# for free and let identical files be recognised without downloading them twice.
# sha256Checksum is a comparatively recent addition to the Drive v3 File
# resource, and Drive rejects an entire request that names a field it does not
# know, so BASIC_FILE_FIELDS is kept as a fallback rather than risking the whole
# listing on it.
FILE_FIELDS = "id, name, mimeType, size, md5Checksum, sha256Checksum"
BASIC_FILE_FIELDS = "id, name, mimeType, size, md5Checksum"


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


def list_files(service, folder_id, parent_path="", fields=None):
    """
    Recursively list all files under the specified folder_id.
    Returns a list of dictionaries with keys: id, name, mimeType, size, relative_path
    and, when Drive provides them, md5Checksum / sha256Checksum.
    Files without size information (e.g. Google Docs) are skipped.

    ``fields`` is a single-element list shared with the recursive calls: if Drive
    refuses the extended field selection the whole run drops to the basic one,
    rather than each subfolder rediscovering the same rejection.
    """
    if fields is None:
        fields = [FILE_FIELDS]
    results = []
    page_token = None
    query = "'{}' in parents".format(folder_id)
    while True:
        try:
            response = (
                service.files()
                .list(
                    q=query,
                    fields=f"nextPageToken, files({fields[0]})",
                    pageToken=page_token,
                    pageSize=1000,
                )
                .execute()
            )
        except Exception:
            if fields[0] == BASIC_FILE_FIELDS:
                raise
            print("Drive rejected the extended field selection; retrying without sha256Checksum")
            fields[0] = BASIC_FILE_FIELDS
            continue

        for f in response.get("files", []):
            file_path = posixpath.join(parent_path, f["name"])
            if f["mimeType"] == "application/vnd.google-apps.folder":
                # Recursively process subfolders
                results.extend(list_files(service, f["id"], file_path, fields))
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


def create_archive(
    service, creds, folder_id, archive_path, max_dict_size=DEFAULT_MAX_DICT_SIZE, deduplicate=True
):
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

    # Parse each size once up front. Files without a usable size are dropped here
    # (instead of crashing the total-size sum) and the parsed value is reused
    # later, avoiding a second int() conversion per file.
    valid_files = []
    for f in files:
        try:
            f["size_bytes"] = int(f["size"])
        except (KeyError, ValueError, TypeError):
            print("Invalid size info for file, skipping:", f.get("relative_path"))
            continue
        valid_files.append(f)
    files = valid_files
    if not files:
        print("No files with valid size information to archive.")
        return False

    total_size = sum(f["size_bytes"] for f in files)

    # Group similar files together and push already-compressed formats to the
    # end, so the solid LZMA stream can exploit redundancy between related files.
    files = order_files(files)

    filters = build_lzma_filters(
        total_size, max_dict_size=max_dict_size, text_ratio=text_byte_ratio(files)
    )
    print(f"Using LZMA dictionary size: {filters[0]['dict_size'] >> 20} MiB")
    if "pb" in filters[0]:
        print("Text-dominated payload detected: using pb=0 for a tighter context model")

    try:
        # Open tar.xz with a custom LZMA2 filter chain for maximum compression.
        # tarfile.open does not accept custom filters, so wrap an LZMAFile and
        # stream the tar into it.
        xz_stream = lzma.LZMAFile(archive_path, mode="wb", format=lzma.FORMAT_XZ, filters=filters)
        tar = tarfile.open(fileobj=xz_stream, mode="w|", format=tarfile.GNU_FORMAT)
    except Exception as e:
        print("Failed to create archive file:", e)
        return False

    # Reuse a single HTTP session so all downloads share one keep-alive
    # connection (one TLS handshake instead of one per file) and set the auth
    # header once. Close the tar before the xz stream (reverse open order) so the
    # archive is finalized and flushed even if an error escapes the loop.
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + creds.token

    def open_stream(entry):
        url = "https://www.googleapis.com/drive/v3/files/{}?alt=media".format(entry["id"])
        try:
            response = session.get(url, stream=True)
            if response.status_code != 200:
                print(
                    "  [ERROR] Failed to download file. HTTP status code:",
                    response.status_code,
                )
                return None
            response.raw.decode_content = True
            return LimitedStream(response.raw, entry["size_bytes"])
        except Exception as e:
            print("  [ERROR] Error while downloading file:", e)
            return None

    with xz_stream, tar, session:
        stats = write_files(tar, files, open_stream, log=print, deduplicate=deduplicate)

    if stats.bytes_deduplicated:
        print(
            f"Deduplicated {stats.files_linked} file(s), "
            f"{stats.bytes_deduplicated} bytes never downloaded or stored "
            f"({stats.bytes_deduplicated * 100 / total_size:.1f}% of the payload)"
        )
    if stats.files_failed:
        print(f"Warning: {stats.files_failed} file(s) could not be archived")

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
        default="auto",
        help="Maximum LZMA dictionary size in MiB, or 'auto' (default) to size "
        "it from the free memory on this machine. A bigger dictionary widens "
        "the window in which LZMA can match data across files, which is what "
        "drives the ratio on large folders, but costs ~10.5x the dictionary "
        "size in RAM while compressing.",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="Do not collapse byte-identical files into hard links. Dedup uses "
        "the checksums Drive already reports, so duplicates are neither "
        "downloaded nor stored; disable it only if your extraction tool "
        "cannot handle hard links.",
    )
    args = parser.parse_args()

    if str(args.max_dict_size_mib).lower() == "auto":
        max_dict_size = auto_max_dict_size()
        print(f"Auto-selected maximum LZMA dictionary size: {max_dict_size >> 20} MiB")
    else:
        try:
            max_dict_size_mib = int(args.max_dict_size_mib)
        except (TypeError, ValueError):
            max_dict_size_mib = 0
        if max_dict_size_mib < 1:
            print("Error: --max-dict-size-mib must be at least 1, or 'auto'")
            sys.exit(1)
        max_dict_size = max_dict_size_mib << 20

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
        max_dict_size=max_dict_size,
        deduplicate=not args.no_dedup,
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


if __name__ == "__main__":  # pragma: no cover
    main()
