"""Extract .swlogdata files from a Shearwater Cloud SQLite database.

Each blob in `log_data.data_bytes_1` is framed as:
    [4 bytes: little-endian uncompressed size][gzip-compressed 32-byte-record stream]

This script strips the prefix, gunzips the body, and writes one .swlogdata file
per dive — the raw 32-byte-record stream that parser.parse() consumes.

Usage:
    python3 extract_blobs.py <dive_data.db> <out_dir>

On macOS the Shearwater Cloud DB lives at:
    ~/Library/Containers/research.shearwater.cloud/Data/Library/Application Support/
      research.shearwater.cloud/users/<email>/dive_data.db
"""
import gzip
import re
import sqlite3
import struct
import sys
from pathlib import Path


def sanitize(name: str) -> str:
    name = re.sub(r"\s+", "_", name.strip())
    name = re.sub(r"[^\w.\-\[\]#@]", "_", name)
    if name.endswith(".swlogzp"):
        name = name[: -len(".swlogzp")]
    return name


def extract(db_path: str, out_dir: str) -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT file_name, data_bytes_1 FROM log_data ORDER BY file_name"
    ).fetchall()

    ok = 0
    for file_name, blob in rows:
        if blob is None or len(blob) < 8:
            print(f"  SKIP {file_name}: empty/short blob")
            continue
        if blob[4:6] != b"\x1f\x8b":
            print(f"  SKIP {file_name}: no gzip magic at offset 4")
            continue
        expected_size = struct.unpack_from("<I", blob, 0)[0]
        try:
            body = gzip.decompress(blob[4:])
        except Exception as e:
            print(f"  FAIL {file_name}: gunzip: {e}")
            continue
        if len(body) % 32 != 0:
            print(f"  WARN {file_name}: decompressed size {len(body)} not a multiple of 32")
            continue
        size_note = "" if expected_size == len(body) else f" (size-prefix mismatch: {expected_size})"
        out_path = out / (sanitize(file_name) + ".swlogdata")
        out_path.write_bytes(body)
        ok += 1
        print(f"  OK  {out_path.name}  ({len(body)} bytes){size_note}")

    print(f"\nextracted {ok}/{len(rows)} blobs to {out}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(extract(sys.argv[1], sys.argv[2]))
