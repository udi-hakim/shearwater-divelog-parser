# shearwater-divelog-parser

A Python parser for Shearwater dive log files (`.swlogdata`).

Reverse-engineered from real exports and validated against the official CSV
exports produced by Shearwater Cloud — every decoded field is checked
sample-by-sample against the CSV, so values are guaranteed to round-trip.

## Status

Validated on three dives (1,583 samples total) across three computer / mode
combinations:

| Computer | Mode | Samples | Fields verified |
|---|---|---|---|
| Petrel 3 | Stand-alone CCR | 668 | 14 / 14 |
| Petrel 3 | Hard-wired JJCCR | 664 | 14 / 14 |
| Tern     | OC nitrox       | 251 | 14 / 14 |

Currently targets **log version 14** (Petrel 3) and **log version 17** (Tern).

## What it decodes

**Per-sample (every ~10 s):**
time, depth, first-stop depth, first-stop time / NDL, TTS, average PPO2,
fraction O2, fraction He, water temperature, battery voltage, external O2
sensor 1/2/3 millivolts.

**Per-dive header / footer:**
dive number, computer serial, product id + name (Petrel 3, Tern, …),
firmware build string and `(major, minor)` version, GF settings, dive start
and end timestamps (UTC), max depth, max time, surface pressure (start /
end), battery voltage (start / end).

**Surfaced but not yet interpreted:**
event markers (`0x51` blocks), tissue saturation snapshots (`0x70`–`0x75`
float groups, ~16 N₂ + 16 He compartments per snapshot), per-sample event
bitmaps (`0x80`–`0x87`, `0xa0`/`0xa1`), tank AI / SAC / ascent rate / CO₂
in the per-sample tail bytes.

## File format in one paragraph

A `.swlogdata` is just the gzipped blob in the Shearwater Cloud SQLite
database (`log_data.data_bytes_1`) with a 4-byte big-endian length header
stripped — but it is also written as a standalone file when you export a
dive. The file is a stream of 32-byte records, where the first byte of each
record is a tag identifying the record type (header TLVs, samples,
events, tissues, footer TLVs, padding). Most fields are little-endian, but
a few — notably depth in the sample record and the dive-start epoch in the
header — are big-endian. Sample timing is implicit at a fixed 10 s
interval; there is no per-sample timestamp.

## Usage

```bash
python3 parser.py <path-to.swlogdata>
```

Prints a decoded header summary (computer, dates, GF, max depth/time,
batteries, pressures) and the first three samples.

To use as a library:

```python
from parser import parse

log = parse("dive.swlogdata")

print(log.header.serial_hex, log.header.product_name)
print(log.header.dive_start_utc, "->", log.header.dive_end_utc)

for s in log.samples:
    print(s.time_sec, s.depth_m, s.average_ppo2, s.water_temp_c)
```

## Validating against a CSV export

If you also export the dive as CSV from Shearwater Cloud, you can verify the
parser against it:

```bash
python3 validate.py <file.swlogdata> <file.csv>
```

Output shows a `[ OK ]` / `[FAIL]` line per field with mismatch counts.

## Reverse-engineering helper

`scan.py` dumps a tag-by-tag map of any `.swlogdata` file plus a count of
each block type — useful when adding support for a new computer or log
version:

```bash
python3 scan.py <file.swlogdata>
```

## Files

- `parser.py` — the parser. `DiveLog`, `DiveHeader`, `Sample`, `Event`, `TissueSnapshot`, `HeaderBitmap` dataclasses and a `parse()` entry point.
- `validate.py` — CSV oracle. Diffs every parsed field against the CSV row.
- `scan.py` — block-by-block hexdump and tag histogram.

## License

MIT — see `LICENSE` if present, otherwise consider the contents under MIT
unless and until a license file is added.

## Disclaimer

This is an independent project. It is **not affiliated with or endorsed by
Shearwater Research**. The file format is undocumented and was decoded by
inspection of public exports; field semantics for unmapped bytes are not
guaranteed.
