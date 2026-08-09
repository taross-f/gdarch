# gdarch compression benchmark

A reproducible harness for measuring what gdarch's archiving strategy is worth.
It drives the real code in `gdarch/compression.py` — the same ordering, filter
selection and deduplication that run against Google Drive — over corpora that
are generated locally from a fixed seed. No network, no credentials, no test
data checked into the repository.

## Running it

```bash
python -m bench.benchmark                       # every corpus, every strategy
python -m bench.benchmark --corpus text mixed   # just these corpora
python -m bench.benchmark --scale 4             # 4x bigger payloads
python -m bench.benchmark --dict-sweep          # what a bigger dictionary buys
python -m bench.benchmark --verify              # also assert exact roundtrip
python -m bench.benchmark --json results.json   # machine-readable output
python -m bench.benchmark --dir ~/Documents     # benchmark your own folder
```

A full `--scale 1` run takes a few minutes: preset 9 with the EXTREME flag is
deliberately slow, and the point of the tool is the ratio, not the throughput.

## What it compares

Each row switches on one change relative to `baseline`, so the table shows where
a win actually comes from rather than just that one exists.

| strategy   | what it is                                                        |
|------------|-------------------------------------------------------------------|
| `baseline` | gdarch 0.2.1: sort by extension then path, preset 9e, no dedup     |
| `ordering` | already-compressed formats moved to the end of the solid stream    |
| `filters`  | `pb=0` when the payload is text-dominated                          |
| `dedup`    | byte-identical files stored as tar hard links, never re-downloaded |
| `improved` | all three together — what ships today                              |

Columns report the archive size, the compression ratio, the change against
`baseline`, wall-clock encode time, and how many payload bytes had to be
downloaded (dedup avoids fetching duplicates at all).

## Corpora

Synthetic, seeded, and deliberately varied — tuning that helps prose does
nothing for a folder of photos.

| corpus    | shape                                                            |
|-----------|------------------------------------------------------------------|
| `text`    | prose documents; what solid LZMA is best at                      |
| `code`    | source files sharing boilerplate; heavy cross-file redundancy    |
| `logs`    | timestamped log lines; repetitive but high-cardinality           |
| `csv`     | numeric exports; structured, weakly redundant                    |
| `office`  | docx-style zip containers; already deflate-compressed            |
| `media`   | photo-sized blobs; incompressible                                |
| `dupes`   | one subtree copied into several sibling folders                  |
| `backups` | a large unique body plus two copies of an old subtree, far apart |
| `mixed`   | a bit of everything: the realistic shared-drive shape            |

## Reading the dictionary sweep

`--dict-sweep` re-runs each corpus across a range of dictionary caps. Real
archives run to many gigabytes while the dictionary is capped at a few hundred
megabytes, so matches between distant copies fall out of range. These corpora
are much smaller, so the same regime is reproduced by *shrinking* the cap: read
the small caps as "archive much larger than the dictionary". That is the regime
where deduplication and incompressible-last ordering pay for themselves, and
where raising the cap (what `--max-dict-size-mib auto` now does) is worth real
percentage points.

## Reference results

Measured with `--scale 1` on Python 3.11, 4 cores, 16 GB RAM. Absolute times
depend on the machine; the deltas do not. Reproduce with
`python -m bench.benchmark` and `python -m bench.benchmark --dict-sweep`.

### Dictionary covers the whole archive (256 MiB cap, these corpora)

| corpus    | baseline  | improved  | archive delta | downloaded delta |
|-----------|-----------|-----------|---------------|------------------|
| `text`    | 1.9 MiB   | 1.9 MiB   | −0.02%        | 0%               |
| `code`    | 12.5 KiB  | 12.3 KiB  | −1.99%        | 0%               |
| `logs`    | 557.1 KiB | 555.7 KiB | −0.26%        | 0%               |
| `csv`     | 934.2 KiB | 931.2 KiB | −0.32%        | 0%               |
| `office`  | 3.2 MiB   | 3.2 MiB   | ±0%           | 0%               |
| `media`   | 6.3 MiB   | 6.3 MiB   | ±0%           | 0%               |
| `dupes`   | 137.3 KiB | 136.0 KiB | −0.97%        | −87%             |
| `backups` | 3.7 MiB   | 3.7 MiB   | −0.07%        | −25%             |
| `mixed`   | 12.6 MiB  | 12.6 MiB  | −0.06%        | 0%               |
| **total** | 29.2 MiB  | 29.2 MiB  | **−0.06%**    | **−11.8%**       |

When every byte of the archive is inside the match window, LZMA already finds
the duplicate content on its own, so deduplication cannot shrink the archive
further — it only stops the bytes being downloaded. What remains is the filter
tuning, worth ~0.3% on text and ~2% on source code.

### Archive larger than the dictionary — the case gdarch is built for

`backups` (22.6 MiB), swept across dictionary caps. Read a 1 MiB cap as "the
folder is 22x the size of the window", which is the everyday situation for a
multi-gigabyte drive against a few hundred megabytes of dictionary.

| dict cap | baseline | improved | delta      |
|----------|----------|----------|------------|
| 1 MiB    | 5.1 MiB  | 3.8 MiB  | **−24.98%** |
| 2 MiB    | 5.0 MiB  | 3.7 MiB  | **−24.95%** |
| 4 MiB    | 4.9 MiB  | 3.7 MiB  | **−24.90%** |
| 8 MiB    | 3.7 MiB  | 3.7 MiB  | −0.07%     |
| 16 MiB   | 3.7 MiB  | 3.7 MiB  | −0.07%     |

The step between 4 and 8 MiB is the window closing over the whole archive. Below
it the codec cannot see the older copies of the subtree and deduplication is
worth a quarter of the archive; above it the codec finds them itself. Both
halves of the change push on this: dedup rescues the left side, and sizing the
dictionary from free memory (up to 1 GiB rather than a fixed 256 MiB) moves the
step four times further to the right.

Where the copies happen to sit close together in the stream, or where there are
no copies at all, the curve is flat instead — the same sweep over `dupes`
(4.5 MiB, sibling folders that sort next to each other) and `mixed` (27.0 MiB,
no duplicate content):

| dict cap | `dupes` delta | `mixed` delta |
|----------|---------------|---------------|
| 1 MiB    | −0.91%        | −0.03%        |
| 2 MiB    | −0.97%        | −0.08%        |
| 4 MiB    | −0.97%        | −0.05%        |
| 8 MiB    | −0.97%        | −0.06%        |
| 16 MiB   | −0.97%        | −0.06%        |

`dupes` still downloads 87% fewer bytes at every cap; what it does not do is get
smaller, because a 1 MiB window is already wide enough to reach the copy. So the
archive-size win is not a property of having duplicates — it is a property of
having duplicates *farther apart than the window*, which is what separates a real
multi-gigabyte drive from a corpus that fits in RAM.

`mixed` is the honest floor: no duplicates, dominated by JPEG and docx payloads
that are already entropy-coded. Nothing here helps it at any window size, beyond
the ~0.05% from filter tuning.
