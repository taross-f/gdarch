import os
import tempfile
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from google.oauth2.credentials import Credentials
from googleapiclient import discovery

from gdarch.cli import (
    LimitedStream,
    create_archive,
    delete_file_or_folder,
    get_credentials,
    get_drive_service,
    get_file_metadata,
    list_files,
    upload_file,
)


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
        "files": [{"id": "file1", "name": "test.txt", "mimeType": "text/plain", "size": "100"}]
    }
    # files().get()のモック設定
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "test123",
        "name": "test_folder",
        "parents": ["parent123"],
    }
    return service


@pytest.fixture
def mock_response():
    response = MagicMock()
    response.status_code = 200
    response.raw = BytesIO(b"test content")
    response.raw.decode_content = True
    return response


def test_get_credentials_from_existing_token(tmp_path):
    # トークンファイルを作成
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "dummy_token"}')

    with patch("google.oauth2.credentials.Credentials.from_authorized_user_file") as mock_from_file:
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_from_file.return_value = mock_creds

        creds = get_credentials(creds_file="dummy_credentials.json", token_file=str(token_file))

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


def test_limited_stream():
    # テストデータの準備
    test_data = b"Hello, World!"
    stream = BytesIO(test_data)
    limit = 5

    # LimitedStreamの作成
    limited = LimitedStream(stream, limit)

    # 制限内での読み取り
    data1 = limited.read(3)
    assert data1 == b"Hel"
    assert limited.remaining == 2

    # 残りのデータを読み取り
    data2 = limited.read()
    assert data2 == b"lo"
    assert limited.remaining == 0

    # 制限を超えた読み取り
    data3 = limited.read()
    assert data3 == b""

    # readable()メソッドのテスト
    assert limited.readable() is True


def test_limited_stream_exact_size():
    # 正確なサイズでの読み取りテスト
    test_data = b"1234567890"
    stream = BytesIO(test_data)
    limit = 10

    limited = LimitedStream(stream, limit)
    data = limited.read()
    assert len(data) == 10
    assert data == test_data


@patch("requests.get")
def test_create_archive_success(mock_get, mock_service, mock_credentials, mock_response, tmp_path):
    # モックの設定
    mock_get.return_value = mock_response

    # テストファイルの作成
    archive_path = tmp_path / "test_archive.tar.xz"

    # アーカイブの作成
    result = create_archive(mock_service, mock_credentials, "test_folder", str(archive_path))

    assert result is True
    assert os.path.exists(archive_path)
    assert os.path.getsize(archive_path) > 0


@patch("requests.get")
def test_create_archive_empty_folder(mock_get, mock_service, mock_credentials, tmp_path):
    # 空のフォルダをシミュレート
    mock_service.files.return_value.list.return_value.execute.return_value = {"files": []}

    # テストファイルの作成
    archive_path = tmp_path / "empty_archive.tar.xz"

    # アーカイブの作成
    result = create_archive(mock_service, mock_credentials, "empty_folder", str(archive_path))

    assert result is False
    assert not os.path.exists(archive_path)


@patch("requests.get")
def test_create_archive_download_error(mock_get, mock_service, mock_credentials, tmp_path):
    # ダウンロードエラーをシミュレート
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_get.return_value = mock_response

    # テストファイルの作成
    archive_path = tmp_path / "error_archive.tar.xz"

    # アーカイブの作成
    result = create_archive(mock_service, mock_credentials, "test_folder", str(archive_path))

    assert result is True  # エラーファイルはスキップされるため、全体としては成功
    assert os.path.exists(archive_path)
