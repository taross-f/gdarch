"""
Compression strategy for gdarch archives.

This module holds everything that decides *how* an archive is built -- how the
files are ordered, which LZMA2 parameters are used, and how duplicate content is
collapsed -- with no dependency on Google Drive. Keeping it separate lets the
benchmark suite under ``bench/`` drive exactly the same code paths against local
corpora, so a claimed compression win can be reproduced offline.
"""

import lzma
import os
import posixpath
import tarfile

# Smallest sensible LZMA2 dictionary size (1 MiB).
MIN_DICT_SIZE = 1 << 20
# Default upper bound for the LZMA2 dictionary size (256 MiB).
# A larger dictionary lets LZMA find matches across the whole solid tar stream
# (i.e. between different files), which improves the compression ratio for big
# folders. Memory usage while compressing is roughly 10.5x the dictionary size,
# so 256 MiB needs ~2.7 GiB of RAM.
DEFAULT_MAX_DICT_SIZE = 256 << 20

# The xz encoder needs roughly this many bytes of RAM per byte of dictionary.
DICT_MEMORY_FACTOR = 10.5
# Only ever spend half of the memory that is actually free on the dictionary.
DICT_MEMORY_SHARE = 0.5
# Bounds for the automatically chosen dictionary cap.
MIN_AUTO_DICT_SIZE = 64 << 20
# 1 GiB is four times the old fixed cap and needs ~11 GiB of RAM to encode;
# beyond it the ratio gain flattens out while the memory bill keeps growing.
MAX_AUTO_DICT_SIZE = 1 << 30

# Formats that are already entropy-coded. Re-compressing them gains nothing, so
# they are pushed to the end of the solid stream where they cannot sit between
# (and split up the match window of) data that does compress.
INCOMPRESSIBLE_EXTENSIONS = frozenset(
    {
        # images
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".heic",
        ".heif",
        ".avif",
        ".jp2",
        ".jxl",
        # audio / video
        ".mp3",
        ".m4a",
        ".aac",
        ".ogg",
        ".oga",
        ".opus",
        ".flac",
        ".wma",
        ".mp4",
        ".m4v",
        ".mov",
        ".mkv",
        ".avi",
        ".webm",
        ".wmv",
        ".flv",
        # containers and archives
        ".zip",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".zst",
        ".lz4",
        ".jar",
        ".apk",
        ".iso",
        ".dmg",
        ".crx",
        # office / publishing formats that are zip or already-compressed
        ".docx",
        ".xlsx",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".epub",
        ".pdf",
    }
)

# Byte-oriented, highly redundant formats. When these dominate the payload the
# LZMA position-bits setting is lowered (see :func:`build_lzma_filters`).
TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".text",
        ".md",
        ".rst",
        ".log",
        ".csv",
        ".tsv",
        ".json",
        ".ndjson",
        ".jsonl",
        ".xml",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".properties",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".less",
        ".js",
        ".mjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".py",
        ".rb",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".php",
        ".swift",
        ".sh",
        ".bash",
        ".zsh",
        ".sql",
        ".r",
        ".m",
        ".pl",
        ".lua",
        ".srt",
        ".vtt",
        ".svg",
        ".tex",
        ".bib",
        ".patch",
        ".diff",
    }
)


def file_extension(path):
    """Lower-cased extension of ``path`` (including the dot), or ``""``."""
    return os.path.splitext(posixpath.basename(path))[1].lower()


def is_incompressible(path):
    """True when ``path`` names a format that is already entropy-coded."""
    return file_extension(path) in INCOMPRESSIBLE_EXTENSIONS


def is_text(path):
    """True when ``path`` names a text-like format."""
    return file_extension(path) in TEXT_EXTENSIONS


def sort_key(relative_path):
    """
    Ordering key for a file inside the solid stream.

    Files are grouped so that (1) everything that can still be compressed comes
    first and already-compressed blobs are appended at the end, (2) within a
    group files of the same type sit next to each other, and (3) ties break on
    the path so sibling files stay adjacent. The first rule keeps incompressible
    payloads from being interleaved with compressible ones, which would push
    related data out of each other's match window.
    """
    return (is_incompressible(relative_path), file_extension(relative_path), relative_path.lower())


def order_files(files):
    """Return ``files`` ordered for the best solid-compression ratio."""
    return sorted(files, key=lambda f: sort_key(f["relative_path"]))


def text_byte_ratio(files):
    """Fraction of the payload (by bytes) that is text-like. 0.0 when empty."""
    total = sum(f["size_bytes"] for f in files)
    if total <= 0:
        return 0.0
    text = sum(f["size_bytes"] for f in files if is_text(f["relative_path"]))
    return text / total


def available_memory():
    """
    Best-effort amount of free RAM in bytes, or ``None`` when unknown.

    Uses the POSIX ``SC_AVPHYS_PAGES`` interface, which reports memory that is
    actually available right now rather than the machine's total.
    """
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None


def auto_max_dict_size(memory=None):
    """
    Pick a dictionary cap that fits in memory instead of a fixed 256 MiB.

    Folders worth archiving are routinely far larger than the old default cap,
    and every byte of dictionary the machine can afford is extra range over
    which LZMA can find cross-file matches. The result is a power of two within
    ``[MIN_AUTO_DICT_SIZE, MAX_AUTO_DICT_SIZE]``; when free memory cannot be
    determined the conservative default is kept.
    """
    if memory is None:
        memory = available_memory()
    if not memory or memory <= 0:
        return DEFAULT_MAX_DICT_SIZE
    budget = int(memory * DICT_MEMORY_SHARE / DICT_MEMORY_FACTOR)
    size = MIN_AUTO_DICT_SIZE
    while size * 2 <= budget and size < MAX_AUTO_DICT_SIZE:
        size *= 2
    if budget < MIN_AUTO_DICT_SIZE:
        return MIN_AUTO_DICT_SIZE
    return min(size, MAX_AUTO_DICT_SIZE)


# Share of text bytes above which pb=0 is used.
TEXT_PB_THRESHOLD = 0.5


def build_lzma_filters(total_size, max_dict_size=DEFAULT_MAX_DICT_SIZE, text_ratio=0.0):
    """
    Build an LZMA2 filter chain tuned for the best possible compression ratio.

    Uses preset 9 with the EXTREME flag (maximum effort, nice_len=273) and grows
    the dictionary to the smallest power of two that covers the entire archive,
    capped at ``max_dict_size``. Sizing the dictionary to the data is what lets
    the codec exploit redundancy across files in the solid tar stream.

    When ``text_ratio`` shows the payload is mostly text, the number of position
    bits is dropped to 0. ``pb`` tells LZMA how many low bits of the offset take
    part in the context model; the default of 2 suits 4-byte-aligned binary
    data, while byte-oriented text has no such alignment and codes better with
    the extra context spent elsewhere.
    """
    dict_size = MIN_DICT_SIZE
    while dict_size < total_size and dict_size < max_dict_size:
        dict_size <<= 1
    # Doubling can overshoot a non-power-of-two cap, so clamp back down.
    dict_size = min(dict_size, max_dict_size)
    lzma2 = {
        "id": lzma.FILTER_LZMA2,
        "preset": 9 | lzma.PRESET_EXTREME,
        "dict_size": dict_size,
    }
    if text_ratio >= TEXT_PB_THRESHOLD:
        lzma2["pb"] = 0
    return [lzma2]


def content_key(entry):
    """
    Identity of a file's *content*, used to detect duplicates.

    Google Drive hands out checksums in the file listing, so identical files can
    be recognised without downloading them. The size is folded into the key so a
    checksum collision alone can never alias two different files.
    """
    digest = entry.get("sha256Checksum") or entry.get("md5Checksum")
    if not digest:
        return None
    return (digest, entry["size_bytes"])


class ArchiveStats:
    """Counters describing what :func:`write_files` actually wrote."""

    def __init__(self):
        self.files_written = 0
        self.files_linked = 0
        self.files_failed = 0
        self.bytes_written = 0
        self.bytes_deduplicated = 0

    @property
    def bytes_total(self):
        return self.bytes_written + self.bytes_deduplicated

    def as_dict(self):
        return {
            "files_written": self.files_written,
            "files_linked": self.files_linked,
            "files_failed": self.files_failed,
            "bytes_written": self.bytes_written,
            "bytes_deduplicated": self.bytes_deduplicated,
        }


def write_files(tar, files, open_stream, log=None, deduplicate=True):
    """
    Write ``files`` into the open ``tar``, collapsing duplicate content.

    ``files`` must already be ordered (see :func:`order_files`) and each entry
    needs ``relative_path``, ``size_bytes`` and optionally a checksum.
    ``open_stream(entry)`` returns a readable stream of the file's bytes, or
    ``None`` if it could not be fetched.

    A file whose content was already stored is emitted as a tar hard link
    pointing at the first copy instead of being downloaded again. Plain
    ``tar -xJf`` restores those links, so the archive stays readable without
    gdarch. This is what rescues folders whose duplicates lie farther apart than
    the dictionary window, where LZMA can no longer match them.
    """
    stats = ArchiveStats()
    stored = {}
    total_size = sum(f["size_bytes"] for f in files) or 1

    for entry in files:
        rel_path = entry["relative_path"]
        size = entry["size_bytes"]
        key = content_key(entry) if deduplicate else None

        if key is not None and key in stored:
            tarinfo = tarfile.TarInfo(name=rel_path)
            tarinfo.type = tarfile.LNKTYPE
            tarinfo.linkname = stored[key]
            tarinfo.size = 0
            tar.addfile(tarinfo)
            stats.files_linked += 1
            stats.bytes_deduplicated += size
            if log:
                log(f"Deduplicated: {rel_path} -> {stored[key]} ({size} bytes saved)")
            continue

        if log:
            done = stats.bytes_total * 100 / total_size
            log(f"Adding to archive: {rel_path} ({size} bytes) - {done:.1f}% complete")

        stream = open_stream(entry)
        if stream is None:
            stats.files_failed += 1
            continue
        try:
            tarinfo = tarfile.TarInfo(name=rel_path)
            tarinfo.size = size
            tar.addfile(tarinfo, fileobj=stream)
        except Exception as e:  # noqa: BLE001 - one bad file must not kill the run
            stats.files_failed += 1
            if log:
                log(f"  [ERROR] Error while adding file to archive: {e}")
            continue

        stats.files_written += 1
        stats.bytes_written += size
        # Only advertise the entry as a link target once it is really in the
        # archive, so a failed download cannot leave dangling hard links behind.
        if key is not None:
            stored[key] = rel_path

    return stats
