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
