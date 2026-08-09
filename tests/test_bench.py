"""Smoke tests for the benchmark harness, so it cannot silently rot."""

import json
import os

import pytest

from bench import benchmark, corpora


def test_every_corpus_is_deterministic_and_non_empty():
    for name in corpora.DEFAULT_CORPORA:
        first = corpora.build(name, scale=1)
        assert first, f"{name} produced no files"
        assert first == corpora.build(name, scale=1)


def test_corpus_scale_grows_the_payload():
    small = sum(len(b) for _, b in corpora.build("code", scale=1))
    large = sum(len(b) for _, b in corpora.build("code", scale=2))
    assert large > small


def test_unknown_corpus_is_rejected():
    with pytest.raises(SystemExit):
        corpora.build("does-not-exist")


def test_to_entries_exposes_sizes_and_checksums():
    entries, blobs = benchmark.to_entries([("a.txt", b"aa"), ("b.txt", b"aa")])
    assert [e["size_bytes"] for e in entries] == [2, 2]
    assert entries[0]["md5Checksum"] == entries[1]["md5Checksum"]
    assert blobs[entries[0]["id"]] == b"aa"


@pytest.mark.parametrize("strategy", sorted(benchmark.STRATEGIES))
def test_every_strategy_produces_a_verifiable_archive(strategy):
    files = [
        ("notes/a.txt", b"hello world " * 200),
        ("notes/b.txt", b"hello world " * 200),  # exact duplicate
        ("photos/c.jpg", bytes(range(256)) * 4),
    ]
    entries, blobs = benchmark.to_entries(files)
    result = benchmark.run_strategy(entries, blobs, strategy, 1 << 20)

    assert result["archive_bytes"] > 0
    assert result["ratio"] > 1
    benchmark.verify(result["archive"], files)

    deduped = benchmark.STRATEGIES[strategy]["dedup"]
    assert result["files_linked"] == (1 if deduped else 0)
    assert (result["downloaded_bytes"] < result["payload_bytes"]) is deduped


def test_verify_rejects_an_archive_that_lost_data():
    files = [("a.txt", b"hello")]
    entries, blobs = benchmark.to_entries(files)
    result = benchmark.run_strategy(entries, blobs, "baseline", 1 << 20)
    with pytest.raises(AssertionError):
        benchmark.verify(result["archive"], files + [("b.txt", b"missing")])


def test_read_dir_walks_a_real_folder(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")
    assert benchmark.read_dir(str(tmp_path)) == [("b.txt", b"b"), ("sub/a.txt", b"a")]


def test_human_readable_sizes():
    assert benchmark.human(512) == "512 B"
    assert benchmark.human(2048) == "2.0 KiB"
    assert benchmark.human(5 << 30) == "5.0 GiB"


def test_main_runs_end_to_end_over_a_directory(tmp_path, capsys):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"repeatable content " * 100)
    (source / "b.txt").write_bytes(b"repeatable content " * 100)
    out = tmp_path / "results.json"

    exit_code = benchmark.main(
        ["--dir", str(source), "--dict-cap-mib", "1", "--verify", "--json", str(out)]
    )

    assert exit_code == 0
    results = json.loads(out.read_text())
    assert {r["strategy"] for r in results} == set(benchmark.STRATEGIES)
    captured = capsys.readouterr().out
    assert "totals" in captured
    assert "bytes downloaded" in captured


def test_main_reports_the_dictionary_sweep(tmp_path, capsys):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"repeatable content " * 100)

    benchmark.main(
        ["--dir", str(source), "--dict-cap-mib", "1", "--dict-sweep", "--sweep-caps", "1", "2"]
    )

    captured = capsys.readouterr().out
    assert "dictionary sweep" in captured


def test_main_skips_an_empty_directory(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    benchmark.main(["--dir", str(empty)])
    assert "empty, skipped" in capsys.readouterr().out


def test_main_runs_a_synthetic_corpus(capsys):
    benchmark.main(
        ["--corpus", "code", "--strategies", "baseline", "improved", "--dict-cap-mib", "1"]
    )
    captured = capsys.readouterr().out
    assert "## code" in captured
    assert "improved" in captured
