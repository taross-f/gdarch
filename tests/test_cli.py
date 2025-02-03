import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from google.oauth2.credentials import Credentials
from googleapiclient import discovery

from gdarch.cli import (create_archive, delete_file_or_folder, get_credentials,
                        get_drive_service, get_file_metadata, list_files,
                        upload_file)


@pytest.fixture
def mock_credentials():
    creds = MagicMock(spec=Credentials)
    creds.valid = True
    creds.token = "dummy_token"
    return creds


@pytest.fixture
def mock_service():
    service = MagicMock()
    # files().list()のモック設定
    files_list = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [
            {"id": "file1", "name": "test.txt", "mimeType": "text/plain", "size": "100"}
        ]
    }
    # files().get()のモック設定
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "test123",
        "name": "test_folder",
        "parents": ["parent123"],
    }
    return service


def test_get_credentials_from_existing_token(tmp_path):
    # トークンファイルを作成
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "dummy_token"}')

    with patch(
        "google.oauth2.credentials.Credentials.from_authorized_user_file"
    ) as mock_from_file:
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_from_file.return_value = mock_creds

        creds = get_credentials(
            creds_file="dummy_credentials.json", token_file=str(token_file)
        )

        assert creds == mock_creds
        mock_from_file.assert_called_once_with(
            str(token_file), ["https://www.googleapis.com/auth/drive"]
        )


@patch("gdarch.cli.build")
def test_get_drive_service(mock_build, mock_credentials):
    mock_service = MagicMock()
    mock_build.return_value = mock_service

    service = get_drive_service(mock_credentials)

    mock_build.assert_called_once_with("drive", "v3", credentials=mock_credentials)


def test_list_files(mock_service):
    files = list_files(mock_service, "root")

    assert len(files) == 1
    assert files[0]["id"] == "file1"
    assert files[0]["relative_path"] == "test.txt"
    assert files[0]["size"] == "100"


def test_get_file_metadata(mock_service):
    metadata = get_file_metadata(mock_service, "test123")

    assert metadata["id"] == "test123"
    assert metadata["name"] == "test_folder"
    assert metadata["parents"] == ["parent123"]

    mock_service.files.return_value.get.assert_called_with(
        fileId="test123", fields="id,name,parents"
    )
