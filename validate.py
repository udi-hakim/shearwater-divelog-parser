"""Validate parser output against CSV export, field by field."""
import csv
import sys
from pathlib import Path

from parser import parse


def read_csv_samples(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    # Row 0 = dive-meta header, row 1 = dive-meta values,
    # Row 2 = sample-column header, rows 3.. = samples.
    sample_cols = rows[2]
    return [dict(zip(sample_cols, r)) for r in rows[3:]]


def main(bin_path: str, csv_path: str) -> None:
    log = parse(bin_path)
    csv_samples = read_csv_samples(Path(csv_path))

    print(f"binary samples: {len(log.samples)}")
    print(f"csv samples:    {len(csv_samples)}")
    n = min(len(log.samples), len(csv_samples))

    mismatches = {
        "time": 0,
        "depth": 0,
        "ppo2": 0,
        "o2": 0,
        "he": 0,
        "ndl": 0,
        "temp": 0,
        "battery": 0,
        "tts": 0,
        "first_stop_depth": 0,
        "first_stop_time": 0,
        "sensor1_mv": 0,
        "sensor2_mv": 0,
        "sensor3_mv": 0,
    }
    first_fail = {}
    for i in range(n):
        b = log.samples[i]
        c = csv_samples[i]
        def check(key: str, b_val, c_val):
            if b_val != c_val:
                mismatches[key] += 1
                first_fail.setdefault(key, (i, b_val, c_val))

        check("time", b.time_sec, int(c["Time (sec)"]))
        check("depth", round(b.depth_m, 1), round(float(c["Depth"]), 1))
        check("first_stop_time", b.first_stop_time_min, int(c["First Stop Time"]))
        check("ppo2", round(b.average_ppo2, 2), round(float(c["Average PPO2"]), 2))
        check("o2", round(b.fraction_o2, 2), round(float(c["Fraction O2"]), 2))
        check("he", round(b.fraction_he, 2), round(float(c["Fraction He"]), 2))
        check("ndl", b.ndl_min, int(c["Current NDL"]))
        check("temp", b.water_temp_c, int(c["Water Temp"]))
        check("battery", round(b.battery_voltage, 2), round(float(c["Battery Voltage"]), 2))
        check("tts", b.tts_min, int(c["Time To Surface (min)"]))
        check("first_stop_depth", round(b.first_stop_depth_m, 1),
              round(float(c["First Stop Depth"]), 1))
        check("sensor1_mv", b.sensor1_mv, int(c["External O2 Sensor 1 (mV)"]))
        check("sensor2_mv", b.sensor2_mv, int(c["External O2 Sensor 2 (mV)"]))
        check("sensor3_mv", b.sensor3_mv, int(c["External O2 Sensor 3 (mV)"]))

    print("\nmismatches (per field):")
    for k, v in mismatches.items():
        tag = " OK " if v == 0 else "FAIL"
        print(f"  [{tag}] {k:20s} {v} / {n}")

    if any(v for v in mismatches.values()):
        print("\nfirst failure per field:")
        for k, (i, b_val, c_val) in first_fail.items():
            print(f"  {k}: sample #{i}  binary={b_val!r}  csv={c_val!r}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
