"""
Deterministic synthetic corpora that stand in for real Google Drive folders.

Everything here is generated from a seeded RNG, so a benchmark run is
reproducible on any machine without shipping test data or touching the network.
Each generator returns ``[(relative_path, contents), ...]``.

The corpora are deliberately different in character, because compression tuning
that helps prose can do nothing for a folder full of photos:

``text``    prose documents -- the case solid LZMA is best at
``code``    source files sharing boilerplate -- heavy cross-file redundancy
``logs``    timestamped log lines -- repetitive but high-cardinality
``csv``     numeric exports -- structured, weakly redundant
``office``  docx-style zip containers -- already deflate-compressed
``media``   photo/video-sized blobs -- incompressible
``dupes``   a subtree copied several times -- exact duplicates
``mixed``   a bit of everything, the realistic "shared drive" shape
``backups`` a large unique body plus copies of an old subtree, laid out so the
            duplicates sit far apart in the stream
"""

import io
import random
import zipfile

WORDS = (
    "the of and to in a is that for it as was with be by on not he i this "
    "are or his from at which but have an they one you were all we can her "
    "has there been if more when will would who so no about out up into them "
    "project report data value result system user file name index total budget "
    "quarter meeting summary revision draft final approved pending customer"
).split()


def _prose(rng, n_words):
    out = []
    for _ in range(n_words):
        out.append(rng.choice(WORDS))
        if rng.random() < 0.06:
            out.append("\n")
    return " ".join(out)


def corpus_text(rng, scale):
    return [
        (f"notes/chapter_{i // 5}/note_{i}.txt", _prose(rng, 30000).encode())
        for i in range(60 * scale)
    ]


def corpus_code(rng, scale):
    header = (
        "#!/usr/bin/env python3\n# Copyright (c) 2024 Example Corp.\n"
        "# Licensed under the MIT License.\n\nimport os\nimport sys\n"
        "import logging\n\nlogger = logging.getLogger(__name__)\n\n"
    )
    template = (
        "class Handler{i}_{j}:\n"
        "    def __init__(self, config):\n"
        "        self.config = config\n"
        "        self.name = 'handler_{i}_{j}'\n\n"
        "    def process(self, payload):\n"
        "        logger.debug('processing %s', self.name)\n"
        "        if not payload:\n"
        "            return None\n"
        "        return {{k: v for k, v in payload.items() if v}}\n\n"
    )
    files = []
    for i in range(400 * scale):
        body = [header]
        for j in range(rng.randint(6, 20)):
            body.append(template.format(i=i, j=j))
        files.append((f"src/module_{i // 10}/handler_{i}.py", "".join(body).encode()))
    return files


def corpus_logs(rng, scale):
    levels = ["INFO", "WARN", "ERROR", "DEBUG"]
    files = []
    for d in range(12 * scale):
        lines = []
        for k in range(4000):
            lines.append(
                f"2024-{1 + d % 12:02d}-{1 + k % 28:02d}T{k % 24:02d}:{k % 60:02d}:{(k * 7) % 60:02d}Z "
                f"[{rng.choice(levels)}] svc-{rng.randint(1, 8)} "
                f"request_id={rng.randint(10**7, 10**8)} "
                f"path=/api/v1/{rng.choice(WORDS)} "
                f"status={rng.choice([200, 200, 200, 404, 500])} "
                f"latency_ms={rng.randint(1, 900)}"
            )
        files.append((f"logs/2024-{1 + d % 12:02d}/app-{d}.log", "\n".join(lines).encode()))
    return files


def corpus_csv(rng, scale):
    files = []
    for i in range(16 * scale):
        rows = ["id,region,product,units,price,total"]
        for k in range(6000):
            units = rng.randint(1, 50)
            price = rng.randint(100, 9999)
            rows.append(
                f"{k},{rng.choice(['east', 'west', 'north', 'south'])},"
                f"SKU-{rng.randint(1000, 9999)},{units},{price / 100:.2f},"
                f"{units * price / 100:.2f}"
            )
        files.append((f"exports/sales_{i}.csv", "\n".join(rows).encode()))
    return files


def corpus_office(rng, scale):
    """DOCX/XLSX are zip containers: their payload is already deflate-compressed."""
    files = []
    for i in range(120 * scale):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            # Fixed timestamps: zip entries otherwise embed "now" and the corpus
            # would differ between runs.
            for member, body in (
                ("[Content_Types].xml", "<?xml version='1.0'?><Types/>"),
                ("word/document.xml", "<w:document>" + _prose(rng, 20000) + "</w:document>"),
            ):
                info = zipfile.ZipInfo(member, date_time=(2024, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, body)
        files.append((f"docs/report_{i}.docx", buf.getvalue()))
    return files


def corpus_media(rng, scale):
    """Photos and video: entropy-coded already, so effectively incompressible."""
    return [
        (f"photos/IMG_{4000 + i}.jpg", rng.randbytes(rng.randint(400_000, 900_000)))
        for i in range(10 * scale)
    ]


def corpus_dupes(rng, scale):
    """The same handful of documents copied into several sibling folders."""
    base = [_prose(rng, 20000).encode() for _ in range(6)]
    return [
        (f"backup_{d}/doc_{i}.txt", blob) for d in range(8 * scale) for i, blob in enumerate(base)
    ]


def corpus_backups(rng, scale):
    """
    A folder that keeps old copies around: a large body of unique documents plus
    two full copies of an older subtree. Sorted into the archive the copies land
    megabytes apart, which is the shape that defeats a fixed-size LZMA window on
    real (multi-gigabyte) drives.
    """
    unique = [(f"current/doc_{i}.txt", _prose(rng, 30000).encode()) for i in range(80 * scale)]
    subtree = [(f"doc_{i}.txt", _prose(rng, 30000).encode()) for i in range(40 * scale)]
    files = list(unique)
    files += [(f"snapshots/2023-01/{p}", b) for p, b in subtree]
    files += [(f"snapshots/2023-06/{p}", b) for p, b in subtree]
    return files


def corpus_mixed(rng, scale):
    """A shared drive: documents, exports, logs, source, office files and photos."""
    files = []
    for name in ("text", "code", "logs", "csv", "office", "media"):
        files.extend(GENERATORS[name](random.Random(rng.randrange(1 << 30)), scale))
    return files


GENERATORS = {
    "text": corpus_text,
    "code": corpus_code,
    "logs": corpus_logs,
    "csv": corpus_csv,
    "office": corpus_office,
    "media": corpus_media,
    "dupes": corpus_dupes,
    "backups": corpus_backups,
    "mixed": corpus_mixed,
}

DEFAULT_CORPORA = ("text", "code", "logs", "csv", "office", "media", "dupes", "backups", "mixed")


def build(name, scale=1, seed=20240501):
    """Generate the named corpus as ``[(relative_path, contents), ...]``."""
    try:
        generator = GENERATORS[name]
    except KeyError:
        raise SystemExit(f"Unknown corpus: {name}. Known: {', '.join(sorted(GENERATORS))}")
    return generator(random.Random(seed), scale)
