# shearwater-divelog-parser

A Python parser for Shearwater dive log files (`.swlogdata`).

Reverse-engineered from real exports and validated against the official CSV
exports produced by Shearwater Cloud — every decoded field is checked
sample-by-sample against the CSV, so values are guaranteed to round-trip.

## Status

Validated against Shearwater Cloud CSV exports on four dives across four
computer / mode combinations — 3,019 samples total, 14 / 14 fields match
exactly on every sample:

| Computer | Mode | Samples | Fields verified |
|---|---|---|---|
| Petrel 3 | Stand-alone CCR   | 668   | 14 / 14 |
| Petrel 3 | Hard-wired JJCCR  | 664   | 14 / 14 |
| Perdix 2 | Stand-alone CCR   | 1,436 | 14 / 14 |
| Tern     | OC nitrox         | 251   | 14 / 14 |

Parses cleanly (0 errors, 0 unmapped record types) on a 120-dive bulk
sample of 56,486 samples covering Petrel 3 SA CCR, Petrel 3 HW JJCCR, and
Tern OC including freedive sessions.

## What it decodes

**Per-sample (every ~10 s):**
time, depth, first-stop depth, first-stop time / NDL, TTS, average PPO2,
fraction O2, fraction He, water temperature (signed), battery voltage,
external O2 sensor 1/2/3 millivolts, status byte (decoded into
OC/SC/CC `dive_mode`, `is_gas_switch`, `setpoint_is_high`,
`has_external_ppo2` flags), setpoint (CCR target PPO2), CNS oxygen
toxicity %.

**Per-dive header / footer:**
dive number, computer serial, product id + name (Petrel 3, Perdix 2,
Tern, …), firmware build string and `(major, minor)` version, GF
settings, dive start and end timestamps (UTC), max depth, max time,
surface pressure (start / end), battery voltage (start / end), units
(metric/imperial), water density (salinity), dive-mode enum (CC / BO,
OC Technical, Gauge, PPO2 Display, SC / BO, CC / BO 2, OC Recreational,
Freedive), battery type (1.5V Alkaline / 1.5V Lithium / 1.2V NiMH /
3.6V Saft / 3.7V Li-Ion), deco model (GF `min/max`, VPM-B `+N`,
VPM-B/GFS `+N %`), gas mixes used (O2 / He / diluent-or-OC, extracted
by scanning samples for unique triples à la libdivecomputer), UTC
offset (minutes, when preserved by the exporter — note that Shearwater
Cloud tends to strip TZ on Petrel/Perdix exports but preserves it on
Tern), DST flag, per-O2-sensor calibration (bar / mV, one value per
calibrated sensor).

**Surfaced but not yet interpreted:**
event markers (`0x51` blocks), tissue saturation snapshots (`0x70`–`0x75`
float groups, ~16 N₂ + 16 He compartments per snapshot), per-sample event
bitmaps (`0x80`–`0x87`, `0xa0`/`0xa1`), tank AI / SAC / ascent rate / CO₂
in the per-sample tail bytes, freedive-sample (`0x02`) internal layout.

## File format in one paragraph

The parser consumes a stream of 32-byte records. That stream lives in the
Shearwater Cloud SQLite database as `log_data.data_bytes_1`, framed as
`[4-byte little-endian uncompressed size][gzip-compressed body]` — use
`extract_blobs.py` to unpack a whole database into one `.swlogdata` file
per dive. The first byte of each 32-byte record is a tag identifying the
record type (header TLVs, samples, events, tissues, footer TLVs,
padding). Most fields are little-endian, but a few — notably depth in the
sample record and the dive-start epoch in the header — are big-endian.
Sample timing is implicit at a fixed 10 s interval; there is no per-sample
timestamp.

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

## Bulk-extracting from Shearwater Cloud

To dump every dive in a Shearwater Cloud database as a separate
`.swlogdata` file (the form `parse()` consumes):

```bash
python3 extract_blobs.py <dive_data.db> <out_dir>
```

On macOS the Cloud database is at
`~/Library/Containers/research.shearwater.cloud/Data/Library/Application Support/research.shearwater.cloud/users/<email>/dive_data.db`.

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
- `extract_blobs.py` — dump every dive in a Shearwater Cloud SQLite DB to a `.swlogdata` file.

## License

MIT — see [`LICENSE`](LICENSE).

## Disclaimer

This is an independent project. It is **not affiliated with or endorsed by
Shearwater Research**. The file format is undocumented and was decoded by
inspection of public exports; field semantics for unmapped bytes are not
guaranteed.
