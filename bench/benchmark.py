"""
Compression benchmark for gdarch.

Runs the real archiving code (:mod:`gdarch.compression`) over reproducible
corpora and reports what each strategy costs and saves, so a claimed
compression improvement can be checked instead of taken on faith.

Usage::

    python -m bench.benchmark                      # default corpora, all strategies
    python -m bench.benchmark --corpus text mixed  # pick corpora
    python -m bench.benchmark --scale 4            # bigger payloads
    python -m bench.benchmark --dir ~/Documents    # benchmark a real folder
    python -m bench.benchmark --dict-sweep         # what dictionary size buys
    python -m bench.benchmark --json results.json  # machine-readable output

Nothing here touches the network or Google Drive: file contents are supplied by
an in-memory reader that stands in for the Drive download, so the ordering,
filter-selection and deduplication code under test is exactly what runs in
production.
"""

import argparse
import hashlib
import io
import json
import lzma
import os
import sys
import tarfile
import time

from bench import corpora
from gdarch.compression import (
    DEFAULT_MAX_DICT_SIZE,
    MIN_DICT_SIZE,
    build_lzma_filters,
    order_files,
    text_byte_ratio,
    write_files,
)

# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------
# "baseline" reproduces gdarch 0.2.1: sort by extension then path, LZMA2 preset
# 9|EXTREME with the dictionary grown to cover the payload, no deduplication.
# The other strategies switch on one improvement at a time so the table shows
# where a win actually comes from, and "improved" is what ships today.


def legacy_order(files):
    return sorted(
        files,
        key=lambda f: (
            os.path.splitext(f["relative_path"])[1].lower(),
            f["relative_path"].lower(),
        ),
    )


def legacy_filters(total_size, max_dict_size, files):
    dict_size = MIN_DICT_SIZE
    while dict_size < total_size and dict_size < max_dict_size:
        dict_size <<= 1
    return [
        {
            "id": lzma.FILTER_LZMA2,
            "preset": 9 | lzma.PRESET_EXTREME,
            "dict_size": min(dict_size, max_dict_size),
        }
    ]


def tuned_filters(total_size, max_dict_size, files):
    return build_lzma_filters(total_size, max_dict_size, text_ratio=text_byte_ratio(files))


STRATEGIES = {
    "baseline": dict(order=legacy_order, filters=legacy_filters, dedup=False, label="gdarch 0.2.1"),
    "ordering": dict(
        order=order_files,
        filters=legacy_filters,
        dedup=False,
        label="+ incompressible-last ordering",
    ),
    "filters": dict(
        order=legacy_order, filters=tuned_filters, dedup=False, label="+ content-aware LZMA filters"
    ),
    "dedup": dict(
        order=legacy_order, filters=legacy_filters, dedup=True, label="+ checksum deduplication"
    ),
    "improved": dict(
        order=order_files, filters=tuned_filters, dedup=True, label="all of the above"
    ),
}


# --------------------------------------------------------------------------
# Corpus plumbing
# --------------------------------------------------------------------------


def to_entries(files):
    """
    Turn ``[(path, contents)]`` into Drive-shaped listing entries.

    The md5 stands in for the ``md5Checksum`` field Drive returns with every
    listing, which is what makes deduplication free of extra downloads.
    """
    entries = []
    blobs = {}
    for index, (path, data) in enumerate(files):
        file_id = str(index)
        blobs[file_id] = data
        entries.append(
            {
                "id": file_id,
                "relative_path": path,
                "size_bytes": len(data),
                "md5Checksum": hashlib.md5(data).hexdigest(),
            }
        )
    return entries, blobs


def read_dir(path):
    """Load a real directory as a corpus, for benchmarking your own data."""
    files = []
    root = os.path.abspath(path)
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            files.append((os.path.relpath(full, root).replace(os.sep, "/"), data))
    return sorted(files)


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def run_strategy(entries, blobs, strategy, max_dict_size):
    """Build one archive and report its size, cost and download volume."""
    spec = STRATEGIES[strategy]
    ordered = spec["order"](list(entries))
    total_size = sum(f["size_bytes"] for f in ordered)
    filters = spec["filters"](total_size, max_dict_size, ordered)

    fetched = {"bytes": 0}

    def open_stream(entry):
        fetched["bytes"] += entry["size_bytes"]
        return io.BytesIO(blobs[entry["id"]])

    buf = io.BytesIO()
    started = time.perf_counter()
    xz_stream = lzma.LZMAFile(buf, mode="wb", format=lzma.FORMAT_XZ, filters=filters)
    tar = tarfile.open(fileobj=xz_stream, mode="w|", format=tarfile.GNU_FORMAT)
    with xz_stream, tar:
        stats = write_files(tar, ordered, open_stream, deduplicate=spec["dedup"])
    elapsed = time.perf_counter() - started

    return {
        "strategy": strategy,
        "label": spec["label"],
        "payload_bytes": total_size,
        "archive_bytes": buf.tell(),
        "ratio": total_size / buf.tell() if buf.tell() else 0.0,
        "seconds": elapsed,
        "downloaded_bytes": fetched["bytes"],
        "files_linked": stats.files_linked,
        "dict_size": filters[0]["dict_size"],
        "pb": filters[0].get("pb"),
        "archive": buf,
    }


def verify(result_archive, files):
    """Extract an archive and confirm it restores the corpus byte for byte."""
    expected = dict(files)
    result_archive.seek(0)
    restored = {}
    with tarfile.open(fileobj=result_archive, mode="r:xz") as tar:
        for member in tar:
            if member.islnk():
                # Hard links carry no payload; tar resolves them to the target.
                restored[member.name] = restored[member.linkname]
            else:
                restored[member.name] = tar.extractfile(member).read()
    if restored != expected:
        missing = set(expected) - set(restored)
        differing = {k for k in expected.keys() & restored.keys() if expected[k] != restored[k]}
        raise AssertionError(
            f"roundtrip mismatch: missing={sorted(missing)[:5]} "
            f"differing={sorted(differing)[:5]}"
        )


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024


def run_corpus(name, files, strategies, max_dict_size, do_verify):
    entries, blobs = to_entries(files)
    payload = sum(len(b) for _, b in files)
    print(f"\n## {name} -- {len(files)} files, {human(payload)}\n")
    header = (
        f"| {'strategy':<10} | {'archive':>11} | {'ratio':>7} | {'vs base':>8} "
        f"| {'time':>7} | {'downloaded':>11} | notes |"
    )
    print(header)
    print("|" + "|".join(["-" * 12, "-" * 13, "-" * 9, "-" * 10, "-" * 9, "-" * 13, "-" * 7]) + "|")

    results = []
    base_bytes = None
    for strategy in strategies:
        result = run_strategy(entries, blobs, strategy, max_dict_size)
        if do_verify:
            verify(result["archive"], files)
        if base_bytes is None:
            base_bytes = result["archive_bytes"]
        result["delta_pct"] = (result["archive_bytes"] - base_bytes) / base_bytes * 100
        notes = []
        if result["files_linked"]:
            notes.append(f"{result['files_linked']} linked")
        if result["pb"] == 0:
            notes.append("pb=0")
        notes.append(f"dict {result['dict_size'] >> 20}M")
        print(
            f"| {strategy:<10} | {human(result['archive_bytes']):>11} | {result['ratio']:>6.2f}x "
            f"| {result['delta_pct']:>+7.2f}% | {result['seconds']:>6.1f}s "
            f"| {human(result['downloaded_bytes']):>11} | {', '.join(notes)} |"
        )
        result.pop("archive")
        result["corpus"] = name
        results.append(result)
    return results


def run_dict_sweep(name, files, caps):
    """
    Show what the dictionary cap is worth.

    Real archives run to many gigabytes while the dictionary is capped at a few
    hundred megabytes, so matches between distant copies fall out of range. These
    corpora are far smaller, so the same regime is reproduced by shrinking the
    cap: read the small caps as "archive much larger than the dictionary".
    """
    entries, blobs = to_entries(files)
    payload = sum(len(b) for _, b in files)
    print(f"\n## dictionary sweep: {name} -- {human(payload)}\n")
    print(f"| {'dict cap':>9} | {'baseline':>11} | {'improved':>11} | {'delta':>8} |")
    print("|" + "|".join(["-" * 11, "-" * 13, "-" * 13, "-" * 10]) + "|")
    results = []
    for cap_mib in caps:
        cap = cap_mib << 20
        base = run_strategy(entries, blobs, "baseline", cap)
        best = run_strategy(entries, blobs, "improved", cap)
        delta = (best["archive_bytes"] - base["archive_bytes"]) / base["archive_bytes"] * 100
        print(
            f"| {cap_mib:>7} M | {human(base['archive_bytes']):>11} "
            f"| {human(best['archive_bytes']):>11} | {delta:>+7.2f}% |"
        )
        for r in (base, best):
            r.pop("archive")
            r["corpus"] = name
            r["dict_cap_mib"] = cap_mib
        results.extend([base, best])
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--corpus",
        nargs="+",
        default=list(corpora.DEFAULT_CORPORA),
        choices=sorted(corpora.GENERATORS) + ["all"],
        help="Synthetic corpora to run (default: all of them).",
    )
    parser.add_argument("--dir", help="Benchmark a real directory instead of synthetic corpora.")
    parser.add_argument(
        "--scale", type=int, default=1, help="Multiply corpus size (default: %(default)s)."
    )
    parser.add_argument("--seed", type=int, default=20240501, help="Corpus RNG seed.")
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=list(STRATEGIES),
        choices=list(STRATEGIES),
        help="Strategies to compare; the first one is the reference row.",
    )
    parser.add_argument(
        "--dict-cap-mib",
        type=int,
        default=DEFAULT_MAX_DICT_SIZE >> 20,
        help="Dictionary cap in MiB, held equal across strategies (default: %(default)s).",
    )
    parser.add_argument(
        "--dict-sweep",
        action="store_true",
        help="Also sweep the dictionary cap to show what a larger dictionary buys.",
    )
    parser.add_argument(
        "--sweep-caps",
        nargs="+",
        type=int,
        default=[1, 2, 4, 8, 16, 32],
        help="Dictionary caps in MiB for --dict-sweep.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Extract every archive and assert it restores the corpus exactly.",
    )
    parser.add_argument("--json", help="Write the raw results to this JSON file.")
    args = parser.parse_args(argv)

    if args.dir:
        datasets = [(os.path.basename(os.path.abspath(args.dir)) or "dir", read_dir(args.dir))]
    else:
        names = sorted(corpora.GENERATORS) if "all" in args.corpus else args.corpus
        datasets = [(n, corpora.build(n, args.scale, args.seed)) for n in names]

    print("# gdarch compression benchmark")
    print(
        f"\npython {sys.version.split()[0]} | dictionary cap {args.dict_cap_mib} MiB | "
        f"scale {args.scale}"
    )

    results = []
    for name, files in datasets:
        if not files:
            print(f"\n## {name} -- empty, skipped")
            continue
        results.extend(
            run_corpus(name, files, args.strategies, args.dict_cap_mib << 20, args.verify)
        )
        if args.dict_sweep:
            results.extend(run_dict_sweep(name, files, args.sweep_caps))

    baselines = {
        r["corpus"]: r for r in results if r["strategy"] == "baseline" and "dict_cap_mib" not in r
    }
    improved = [r for r in results if r["strategy"] == "improved" and "dict_cap_mib" not in r]
    if improved:
        total_base = sum(baselines[r["corpus"]]["archive_bytes"] for r in improved)
        total_best = sum(r["archive_bytes"] for r in improved)
        total_payload = sum(r["payload_bytes"] for r in improved)
        dl_base = sum(baselines[r["corpus"]]["downloaded_bytes"] for r in improved)
        dl_best = sum(r["downloaded_bytes"] for r in improved)
        print("\n## totals\n")
        print(f"payload           {human(total_payload)}")
        print(f"baseline archive  {human(total_base)}  ({total_payload / total_base:.2f}x)")
        print(f"improved archive  {human(total_best)}  ({total_payload / total_best:.2f}x)")
        print(f"archive delta     {(total_best - total_base) / total_base:+.2%}")
        print(
            f"bytes downloaded  {human(dl_base)} -> {human(dl_best)} "
            f"({(dl_best - dl_base) / dl_base:+.2%})"
        )

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
