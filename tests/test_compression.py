import io
import lzma
import tarfile

import pytest

from gdarch.compression import (
    DEFAULT_MAX_DICT_SIZE,
    MAX_AUTO_DICT_SIZE,
    MIN_AUTO_DICT_SIZE,
    MIN_DICT_SIZE,
    ArchiveStats,
    auto_max_dict_size,
    available_memory,
    build_lzma_filters,
    content_key,
    file_extension,
    is_incompressible,
    is_text,
    order_files,
    sort_key,
    text_byte_ratio,
    write_files,
)


def entry(path, size=10, **kw):
    e = {"relative_path": path, "size_bytes": size, "id": path}
    e.update(kw)
    return e


# --- classification ------------------------------------------------------


def test_file_extension_is_lowercased_and_basename_only():
    assert file_extension("dir.txt/report.PDF") == ".pdf"
    assert file_extension("noext") == ""


def test_incompressible_and_text_classification():
    assert is_incompressible("photos/IMG_1.JPG")
    assert is_incompressible("a/b.docx")
    assert not is_incompressible("a/b.txt")
    assert is_text("notes/a.md")
    assert not is_text("photos/a.jpg")


def test_sort_key_puts_incompressible_last():
    # .jpg sorts before .txt alphabetically, but must land at the end
    assert sort_key("a.txt") < sort_key("a.jpg")


def test_order_files_groups_by_class_then_extension_then_path():
    files = [
        entry("z.jpg"),
        entry("b.txt"),
        entry("a.md"),
        entry("sub/a.txt"),
        entry("a.zip"),
    ]
    assert [f["relative_path"] for f in order_files(files)] == [
        "a.md",
        "b.txt",
        "sub/a.txt",
        "z.jpg",
        "a.zip",
    ]


def test_text_byte_ratio():
    files = [entry("a.txt", 75), entry("b.jpg", 25)]
    assert text_byte_ratio(files) == 0.75
    assert text_byte_ratio([]) == 0.0
    assert text_byte_ratio([entry("a.txt", 0)]) == 0.0


# --- filters -------------------------------------------------------------


def test_build_lzma_filters_uses_max_effort_preset():
    filters = build_lzma_filters(1024)
    assert len(filters) == 1
    assert filters[0]["id"] == lzma.FILTER_LZMA2
    assert filters[0]["preset"] == (9 | lzma.PRESET_EXTREME)
    assert filters[0]["dict_size"] == MIN_DICT_SIZE


def test_build_lzma_filters_rounds_dictionary_up_and_clamps():
    assert build_lzma_filters(5 * MIN_DICT_SIZE)[0]["dict_size"] == 8 * MIN_DICT_SIZE
    cap = 768 << 20
    assert build_lzma_filters(cap * 16, max_dict_size=cap)[0]["dict_size"] == cap


def test_build_lzma_filters_lowers_position_bits_for_text():
    assert "pb" not in build_lzma_filters(1024, text_ratio=0.1)[0]
    assert build_lzma_filters(1024, text_ratio=0.9)[0]["pb"] == 0


def test_tuned_filters_are_accepted_by_the_encoder():
    filters = build_lzma_filters(1 << 20, text_ratio=1.0)
    payload = b"hello world " * 5000
    assert (
        lzma.decompress(lzma.compress(payload, format=lzma.FORMAT_XZ, filters=filters)) == payload
    )


# --- memory-aware dictionary cap -----------------------------------------


def test_auto_max_dict_size_scales_with_memory():
    small = auto_max_dict_size(2 << 30)
    large = auto_max_dict_size(64 << 30)
    assert small < large
    assert small >= MIN_AUTO_DICT_SIZE
    assert large <= MAX_AUTO_DICT_SIZE
    assert large & (large - 1) == 0  # power of two


def test_auto_max_dict_size_floors_on_tiny_machines():
    assert auto_max_dict_size(64 << 20) == MIN_AUTO_DICT_SIZE


def test_auto_max_dict_size_falls_back_when_memory_unknown():
    assert auto_max_dict_size(0) == DEFAULT_MAX_DICT_SIZE
    assert auto_max_dict_size(None) > 0


def test_auto_max_dict_size_uses_available_memory_by_default(monkeypatch):
    monkeypatch.setattr("gdarch.compression.available_memory", lambda: 8 << 30)
    assert auto_max_dict_size() == auto_max_dict_size(8 << 30)


def test_available_memory_returns_none_when_unsupported(monkeypatch):
    def boom(_name):
        raise ValueError("unsupported")

    monkeypatch.setattr("gdarch.compression.os.sysconf", boom)
    assert available_memory() is None


def test_available_memory_on_this_platform():
    memory = available_memory()
    assert memory is None or memory > 0


# --- content keys --------------------------------------------------------


def test_content_key_prefers_sha256_and_folds_in_size():
    assert content_key(entry("a", 5, md5Checksum="m")) == ("m", 5)
    assert content_key(entry("a", 5, md5Checksum="m", sha256Checksum="s")) == ("s", 5)
    assert content_key(entry("a", 5)) is None


# --- archive assembly ----------------------------------------------------


def build(files, blobs, **kw):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w|", format=tarfile.GNU_FORMAT) as tar:
        stats = write_files(tar, files, lambda e: io.BytesIO(blobs[e["id"]]), **kw)
    buf.seek(0)
    return buf, stats


def read_back(buf):
    out = {}
    with tarfile.open(fileobj=buf, mode="r|") as tar:
        for member in tar:
            out[member.name] = (
                out[member.linkname] if member.islnk() else tar.extractfile(member).read()
            )
    return out


def test_write_files_deduplicates_identical_content():
    blobs = {"a": b"same", "b": b"same", "c": b"other"}
    files = [
        entry("a.txt", 4, id="a", md5Checksum="x"),
        entry("b.txt", 4, id="b", md5Checksum="x"),
        entry("c.txt", 5, id="c", md5Checksum="y"),
    ]
    buf, stats = build(files, blobs)

    assert stats.files_written == 2
    assert stats.files_linked == 1
    assert stats.bytes_deduplicated == 4
    assert stats.bytes_total == 13
    assert read_back(buf) == {"a.txt": b"same", "b.txt": b"same", "c.txt": b"other"}


def test_write_files_emits_a_hard_link_for_the_duplicate():
    blobs = {"a": b"same", "b": b"same"}
    files = [
        entry("a.txt", 4, id="a", md5Checksum="x"),
        entry("b.txt", 4, id="b", md5Checksum="x"),
    ]
    buf, _ = build(files, blobs)
    with tarfile.open(fileobj=buf, mode="r|") as tar:
        members = list(tar)
    assert members[0].isreg()
    assert members[1].islnk()
    assert members[1].linkname == "a.txt"
    assert members[1].size == 0


def test_write_files_can_disable_dedup():
    blobs = {"a": b"same", "b": b"same"}
    files = [
        entry("a.txt", 4, id="a", md5Checksum="x"),
        entry("b.txt", 4, id="b", md5Checksum="x"),
    ]
    _, stats = build(files, blobs, deduplicate=False)
    assert stats.files_linked == 0
    assert stats.files_written == 2


def test_write_files_never_links_to_a_file_that_failed_to_download():
    # The first copy cannot be fetched, so the second must be stored for real
    # rather than becoming a hard link to a member that is not in the archive.
    blobs = {"b": b"same"}
    files = [
        entry("a.txt", 4, id="a", md5Checksum="x"),
        entry("b.txt", 4, id="b", md5Checksum="x"),
    ]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w|", format=tarfile.GNU_FORMAT) as tar:
        stats = write_files(
            tar, files, lambda e: io.BytesIO(blobs[e["id"]]) if e["id"] in blobs else None
        )
    buf.seek(0)
    assert stats.files_failed == 1
    assert stats.files_linked == 0
    assert read_back(buf) == {"b.txt": b"same"}


def test_write_files_skips_files_without_checksums():
    blobs = {"a": b"same", "b": b"same"}
    files = [entry("a.txt", 4, id="a"), entry("b.txt", 4, id="b")]
    _, stats = build(files, blobs)
    assert stats.files_linked == 0


def test_write_files_reports_and_survives_a_broken_stream():
    class Exploding:
        def read(self, size=-1):
            raise OSError("connection reset")

    files = [entry("a.txt", 4, id="a"), entry("b.txt", 4, id="b")]
    blobs = {"b": b"fine"}
    logged = []
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w|", format=tarfile.GNU_FORMAT) as tar:
        stats = write_files(
            tar,
            files,
            lambda e: Exploding() if e["id"] == "a" else io.BytesIO(blobs[e["id"]]),
            log=logged.append,
        )
    assert stats.files_failed == 1
    assert stats.files_written == 1
    assert any("ERROR" in line for line in logged)


def test_write_files_logs_progress_and_dedup():
    blobs = {"a": b"same", "b": b"same"}
    files = [
        entry("a.txt", 4, id="a", md5Checksum="x"),
        entry("b.txt", 4, id="b", md5Checksum="x"),
    ]
    logged = []
    build(files, blobs, log=logged.append)
    assert any("Adding to archive" in line for line in logged)
    assert any("Deduplicated" in line for line in logged)


def test_write_files_handles_an_empty_file_list():
    buf, stats = build([], {})
    assert stats.files_written == 0
    assert stats.bytes_total == 0


def test_archive_stats_as_dict():
    stats = ArchiveStats()
    stats.files_written = 1
    assert stats.as_dict()["files_written"] == 1
