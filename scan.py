"""Scan a .swlogdata file and print a block-by-block tag map."""
import sys
from collections import Counter
from pathlib import Path

BLOCK = 32


def scan(path: Path) -> None:
    data = path.read_bytes()
    assert len(data) % BLOCK == 0, f"file size {len(data)} not multiple of {BLOCK}"
    tags = Counter()
    print(f"{'off':>6}  tag  hexdump")
    for off in range(0, len(data), BLOCK):
        block = data[off:off + BLOCK]
        tag = block[0]
        tags[tag] += 1
        if off < 0x400 or tag not in (0x01,):
            print(f"{off:#06x}  {tag:#04x}  {block.hex()}")
    print()
    print("tag counts:")
    for tag, n in sorted(tags.items()):
        print(f"  {tag:#04x}  {n}")


if __name__ == "__main__":
    scan(Path(sys.argv[1]))
