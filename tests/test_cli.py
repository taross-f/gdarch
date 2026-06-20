import lzma
import os
import tarfile
import tempfile
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from google.oauth2.credentials import Credentials
from googleapiclient import discovery

from gdarch.cli import (
    DEFAULT_MAX_DICT_SIZE,
    MIN_DICT_SIZE,
    LimitedStream,
    build_lzma_filters,
    create_archive,
    delete_file_or_folder,
    get_credentials,
    get_drive_service,
    get_file_metadata,
    list_files,
    main,
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


def test_get_credentials_refresh_token(tmp_path):
    # 期限切れのトークンをシミュレート
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "expired_token"}')

    with patch("google.oauth2.credentials.Credentials.from_authorized_user_file") as mock_from_file:
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = True
        mock_from_file.return_value = mock_creds

        with patch.object(mock_creds, "refresh") as mock_refresh:
            creds = get_credentials(creds_file="dummy_credentials.json", token_file=str(token_file))
            mock_refresh.assert_called_once()


def test_get_credentials_new_flow(tmp_path):
    # トークンが存在しない場合をシミュレート
    token_file = tmp_path / "token.json"

    with patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file") as mock_flow:
        mock_creds = MagicMock()
        mock_creds.to_json.return_value = '{"token": "dummy_token"}'  # to_json()の戻り値を設定
        mock_flow.return_value.run_local_server.return_value = mock_creds

        creds = get_credentials(creds_file="dummy_credentials.json", token_file=str(token_file))

        mock_flow.assert_called_once_with(
            "dummy_credentials.json", ["https://www.googleapis.com/auth/drive"]
        )
        assert os.path.exists(token_file)


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


def test_list_files_with_folder(mock_service):
    # フォルダを含むケースをシミュレート
    mock_service.files.return_value.list.return_value.execute.side_effect = [
        {
            "files": [
                {"id": "file1", "name": "test.txt", "mimeType": "text/plain", "size": "100"},
                {
                    "id": "folder1",
                    "name": "subfolder",
                    "mimeType": "application/vnd.google-apps.folder",
                },
            ]
        },
        {
            "files": [
                {"id": "file2", "name": "subfile.txt", "mimeType": "text/plain", "size": "200"}
            ]
        },
    ]

    files = list_files(mock_service, "root")

    assert len(files) == 2
    assert files[0]["relative_path"] == "test.txt"
    assert files[1]["relative_path"] == "subfolder/subfile.txt"


def test_get_file_metadata(mock_service):
    metadata = get_file_metadata(mock_service, "test123")

    assert metadata["id"] == "test123"
    assert metadata["name"] == "test_folder"
    assert metadata["parents"] == ["parent123"]

    mock_service.files.return_value.get.assert_called_with(
        fileId="test123", fields="id,name,parents"
    )


def test_upload_file(mock_service):
    mock_service.files.return_value.create.return_value.execute.return_value = {"id": "uploaded123"}

    with tempfile.NamedTemporaryFile() as tmp_file:
        tmp_file.write(b"test content")
        tmp_file.flush()

        file_id = upload_file(mock_service, tmp_file.name, "test.tar.xz", "parent123")

        assert file_id == "uploaded123"
        mock_service.files.return_value.create.assert_called_once()


def test_delete_file_or_folder(mock_service):
    delete_file_or_folder(mock_service, "file123")
    mock_service.files.return_value.delete.assert_called_once_with(fileId="file123")


def test_delete_file_or_folder_error(mock_service):
    mock_service.files.return_value.delete.side_effect = Exception("Delete failed")
    delete_file_or_folder(mock_service, "file123")  # エラーがキャッチされることを確認


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


@patch("requests.Session.get")
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


@patch("requests.Session.get")
def test_create_archive_empty_folder(mock_get, mock_service, mock_credentials, tmp_path):
    # 空のフォルダをシミュレート
    mock_service.files.return_value.list.return_value.execute.return_value = {"files": []}

    # テストファイルの作成
    archive_path = tmp_path / "empty_archive.tar.xz"

    # アーカイブの作成
    result = create_archive(mock_service, mock_credentials, "empty_folder", str(archive_path))

    assert result is False
    assert not os.path.exists(archive_path)


@patch("requests.Session.get")
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


@patch("gdarch.cli.lzma.LZMAFile", side_effect=OSError("disk full"))
def test_create_archive_open_failure(mock_lzma, mock_service, mock_credentials, tmp_path):
    # アーカイブファイルのオープンに失敗したら False を返す
    archive_path = tmp_path / "open_fail.tar.xz"

    result = create_archive(mock_service, mock_credentials, "test_folder", str(archive_path))

    assert result is False


@patch("requests.Session.get")
@patch("gdarch.cli.list_files")
def test_create_archive_skips_invalid_size(
    mock_list_files, mock_get, mock_service, mock_credentials, mock_response, tmp_path
):
    # サイズが数値でないファイルはスキップし、残りはアーカイブされる
    mock_list_files.return_value = [
        {"id": "bad", "relative_path": "broken.txt", "size": "not-a-number"},
        {"id": "ok", "relative_path": "good.txt", "size": "12"},
    ]
    mock_get.return_value = mock_response

    archive_path = tmp_path / "skip_invalid.tar.xz"
    result = create_archive(mock_service, mock_credentials, "test_folder", str(archive_path))

    assert result is True
    with tarfile.open(archive_path, "r:xz") as tar:
        assert tar.getnames() == ["good.txt"]


@patch("gdarch.cli.list_files")
def test_create_archive_all_invalid_sizes(
    mock_list_files, mock_service, mock_credentials, tmp_path
):
    # 全ファイルのサイズが不正なら False を返す
    mock_list_files.return_value = [
        {"id": "bad", "relative_path": "broken.txt", "size": "oops"},
    ]
    archive_path = tmp_path / "all_invalid.tar.xz"

    result = create_archive(mock_service, mock_credentials, "test_folder", str(archive_path))

    assert result is False
    assert not os.path.exists(archive_path)


@patch("gdarch.cli.get_credentials")
@patch("gdarch.cli.get_drive_service")
@patch("gdarch.cli.create_archive")
@patch("gdarch.cli.upload_file")
def test_main_success(mock_upload, mock_create, mock_drive_service, mock_creds, mock_service):
    # モックの設定
    mock_creds.return_value = MagicMock()
    mock_drive_service.return_value = mock_service
    mock_create.return_value = True
    mock_upload.return_value = "uploaded123"

    # コマンドライン引数をシミュレート
    test_args = ["--folder-id", "test123", "--credentials", "test_creds.json"]
    with patch("sys.argv", ["gdarch"] + test_args):
        main()

    mock_create.assert_called_once()
    mock_upload.assert_called_once()


@patch("gdarch.cli.get_credentials")
@patch("gdarch.cli.get_drive_service")
def test_main_no_parent_folder(mock_drive_service, mock_creds, mock_service):
    # 親フォルダがない場合をシミュレート
    mock_creds.return_value = MagicMock()
    mock_drive_service.return_value = mock_service
    mock_service.files.return_value.get.return_value.execute.return_value = {
        "id": "test123",
        "name": "test_folder",
    }

    # コマンドライン引数をシミュレート
    test_args = ["--folder-id", "test123", "--credentials", "test_creds.json"]
    with patch("sys.argv", ["gdarch"] + test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


@patch("gdarch.cli.get_credentials")
@patch("gdarch.cli.get_drive_service")
@patch("gdarch.cli.create_archive")
def test_main_archive_creation_failed(mock_create, mock_drive_service, mock_creds, mock_service):
    # アーカイブ作成失敗をシミュレート
    mock_creds.return_value = MagicMock()
    mock_drive_service.return_value = mock_service
    mock_create.return_value = False

    # コマンドライン引数をシミュレート
    test_args = ["--folder-id", "test123", "--credentials", "test_creds.json"]
    with patch("sys.argv", ["gdarch"] + test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


def test_get_credentials_from_token_json():
    # トークンJSONからの認証をテスト
    token_json = """{
        "token": "dummy_token",
        "refresh_token": "dummy_refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "dummy_client_id",
        "client_secret": "dummy_secret",
        "scopes": ["https://www.googleapis.com/auth/drive"]
    }"""

    with patch("google.oauth2.credentials.Credentials.from_authorized_user_info") as mock_from_info:
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_from_info.return_value = mock_creds

        creds = get_credentials(token_json=token_json)

        assert creds == mock_creds
        mock_from_info.assert_called_once()


def test_build_lzma_filters_structure():
    # フィルタは最大圧縮設定のLZMA2チェーンを返す
    filters = build_lzma_filters(1024)
    assert len(filters) == 1
    assert filters[0]["id"] == lzma.FILTER_LZMA2
    assert filters[0]["preset"] == (9 | lzma.PRESET_EXTREME)


def test_build_lzma_filters_uses_minimum_for_small_data():
    # 小さいデータでは最小辞書サイズが使われる
    assert build_lzma_filters(0)[0]["dict_size"] == MIN_DICT_SIZE
    assert build_lzma_filters(1)[0]["dict_size"] == MIN_DICT_SIZE
    assert build_lzma_filters(MIN_DICT_SIZE)[0]["dict_size"] == MIN_DICT_SIZE


def test_build_lzma_filters_rounds_up_to_power_of_two():
    # データを覆う最小の2の冪に切り上げる
    assert build_lzma_filters(5 * MIN_DICT_SIZE)[0]["dict_size"] == 8 * MIN_DICT_SIZE
    assert build_lzma_filters(MIN_DICT_SIZE + 1)[0]["dict_size"] == 2 * MIN_DICT_SIZE


def test_build_lzma_filters_caps_at_max():
    # 巨大なデータでも上限を超えない
    huge = DEFAULT_MAX_DICT_SIZE * 16
    assert build_lzma_filters(huge)[0]["dict_size"] == DEFAULT_MAX_DICT_SIZE


def test_build_lzma_filters_clamps_non_power_of_two_cap():
    # 非2冪の上限でも、倍々で超過した分はクランプされる
    cap = 768 << 20  # 512 + 256 MiB (2の冪ではない)
    huge = cap * 16
    assert build_lzma_filters(huge, max_dict_size=cap)[0]["dict_size"] == cap


@patch("gdarch.cli.get_credentials")
@patch("gdarch.cli.get_drive_service")
def test_main_invalid_max_dict_size(mock_drive_service, mock_creds, mock_service):
    # --max-dict-size-mib が1未満なら終了コード1で終了する
    mock_creds.return_value = MagicMock()
    mock_drive_service.return_value = mock_service

    test_args = [
        "--folder-id",
        "test123",
        "--credentials",
        "test_creds.json",
        "--max-dict-size-mib",
        "0",
    ]
    with patch("sys.argv", ["gdarch"] + test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


@patch("requests.Session.get")
def test_create_archive_respects_max_dict_size(
    mock_get, mock_service, mock_credentials, mock_response, tmp_path
):
    # カスタムの max_dict_size を渡してもアーカイブが作成される
    mock_get.return_value = mock_response
    archive_path = tmp_path / "custom_dict.tar.xz"

    result = create_archive(
        mock_service,
        mock_credentials,
        "test_folder",
        str(archive_path),
        max_dict_size=MIN_DICT_SIZE,
    )

    assert result is True
    assert os.path.exists(archive_path)
    assert os.path.getsize(archive_path) > 0


def test_get_credentials_invalid_token_json():
    # 無効なトークンJSONの処理をテスト
    token_json = "invalid json"

    with patch("google.oauth2.credentials.Credentials.from_authorized_user_info") as mock_from_info:
        creds = get_credentials(token_json=token_json)
        assert creds is None
        mock_from_info.assert_not_called()


def _download_by_file_id(contents):
    """contents: {file_id: bytes} -> requests.get の side_effect を返す。

    create_archive が組み立てる /files/<id>?alt=media の URL から file_id を
    取り出し、対応する内容をストリームとして返す。
    """

    def fake_get(url, headers=None, stream=False):
        file_id = url.split("/files/")[1].split("?")[0]
        response = MagicMock()
        response.status_code = 200
        response.raw = BytesIO(contents[file_id])
        response.raw.decode_content = True
        return response

    return fake_get


@patch("requests.Session.get")
@patch("gdarch.cli.list_files")
def test_create_archive_sorts_files_by_extension_then_path(
    mock_list_files, mock_get, mock_service, mock_credentials, tmp_path
):
    # 拡張子→パスの順に並べ替えられ、アーカイブ内の並びへ反映される
    mock_list_files.return_value = [
        {"id": "b", "relative_path": "b.txt", "size": "3"},
        {"id": "a", "relative_path": "a.md", "size": "2"},
        {"id": "c", "relative_path": "sub/c.txt", "size": "4"},
    ]
    contents = {"a": b"AA", "b": b"BBB", "c": b"CCCC"}
    mock_get.side_effect = _download_by_file_id(contents)

    archive_path = tmp_path / "sorted.tar.xz"
    result = create_archive(mock_service, mock_credentials, "test_folder", str(archive_path))

    assert result is True
    with tarfile.open(archive_path, "r:xz") as tar:
        names = tar.getnames()
    # .md (a.md) -> .txt (b.txt) -> .txt (sub/c.txt) の順
    assert names == ["a.md", "b.txt", "sub/c.txt"]


@patch("requests.Session.get")
@patch("gdarch.cli.list_files")
def test_create_archive_roundtrip_preserves_contents_and_paths(
    mock_list_files, mock_get, mock_service, mock_credentials, tmp_path
):
    # 展開すると元のパス構造と内容が完全に復元される
    mock_list_files.return_value = [
        {"id": "root", "relative_path": "readme.txt", "size": "5"},
        {"id": "nested", "relative_path": "dir/sub/data.bin", "size": "6"},
    ]
    contents = {"root": b"hello", "nested": b"\x00\x01\x02\x03\x04\x05"}
    mock_get.side_effect = _download_by_file_id(contents)

    archive_path = tmp_path / "roundtrip.tar.xz"
    result = create_archive(mock_service, mock_credentials, "test_folder", str(archive_path))

    assert result is True
    with tarfile.open(archive_path, "r:xz") as tar:
        extracted = {member.name: tar.extractfile(member).read() for member in tar.getmembers()}

    assert extracted == {
        "readme.txt": b"hello",
        "dir/sub/data.bin": b"\x00\x01\x02\x03\x04\x05",
    }


def test_list_files_skips_files_without_size(mock_service):
    # サイズ情報のないファイル(Googleドキュメント等)はスキップされる
    mock_service.files.return_value.list.return_value.execute.return_value = {
        "files": [
            {"id": "doc1", "name": "doc.gdoc", "mimeType": "application/vnd.google-apps.document"},
            {"id": "file1", "name": "real.txt", "mimeType": "text/plain", "size": "10"},
        ]
    }

    files = list_files(mock_service, "root")

    assert len(files) == 1
    assert files[0]["id"] == "file1"


def test_get_credentials_refresh_failure(tmp_path):
    # リフレッシュに失敗し、フォールバックする認証情報もなければ None を返す
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "expired_token"}')

    with patch("google.oauth2.credentials.Credentials.from_authorized_user_file") as mock_from_file:
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = True
        mock_creds.refresh.side_effect = Exception("refresh failed")
        mock_from_file.return_value = mock_creds

        creds = get_credentials(creds_file=None, token_file=str(token_file))

        assert creds is None


def test_main_requires_credentials_or_token():
    # --credentials も --token も無ければ終了コード1で終了する
    with patch("sys.argv", ["gdarch", "--folder-id", "test123"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


@patch("gdarch.cli.get_credentials")
def test_main_invalid_credentials(mock_creds):
    # 認証情報を取得できなければ終了コード1で終了する
    mock_creds.return_value = None
    with patch("sys.argv", ["gdarch", "--folder-id", "test123", "--credentials", "c.json"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


@patch("gdarch.cli.delete_file_or_folder")
@patch("gdarch.cli.get_credentials")
@patch("gdarch.cli.get_drive_service")
@patch("gdarch.cli.create_archive")
@patch("gdarch.cli.upload_file")
def test_main_delete_folder(
    mock_upload, mock_create, mock_drive_service, mock_creds, mock_delete, mock_service
):
    # --delete-folder 指定時は元フォルダを削除する
    mock_creds.return_value = MagicMock()
    mock_drive_service.return_value = mock_service
    mock_create.return_value = True
    mock_upload.return_value = "uploaded123"

    test_args = ["--folder-id", "test123", "--credentials", "c.json", "--delete-folder"]
    with patch("sys.argv", ["gdarch"] + test_args):
        main()

    mock_delete.assert_called_once_with(mock_service, "test123")
