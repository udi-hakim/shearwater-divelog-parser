"""Parser for Shearwater .swlogdata files (Petrel 3 and Tern).

The file is a stream of 32-byte records. The first byte of each record is a tag.

Record layout (verified against 120 dives on Petrel 3 + Tern):

  0x10-0x19    Header TLV blocks (dive metadata, gas list, settings, etc.)
  0x20-0x29    Footer TLV blocks (end-of-dive mirror of header)
  0x80-0x87    Bitmap blocks (per-sample event flags, 32 bits each)
  0xa0-0xa1    Bitmap blocks (additional flags)
  0x01         Per-sample record (~every 10 s, scuba profile)
  0x02         Freedive sample record (Tern freedive mode; layout not yet mapped)
  0x30         Timestamp/state block (carries dive-start epoch)
  0x51         Event marker (gas switch, setpoint change, etc.)
  0x70-0x75    Tissue saturation snapshot (one group per snapshot, 16 compartments)
  0xff         End-of-file / footer delimiter

The caller gets a `DiveLog` dataclass plus the raw block stream for fields we
have not yet mapped.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BLOCK_SIZE = 32
SAMPLE_INTERVAL_SEC = 10  # confirmed from CSV (time column increments by 10)


@dataclass
class Sample:
    time_sec: int
    depth_m: float
    first_stop_depth_m: int           # whole meters
    tts_min: int
    average_ppo2: float
    fraction_o2: float
    fraction_he: float
    ndl_or_first_stop_min: int        # NDL when first_stop_depth_m == 0, else first-stop time
    water_temp_c: int
    battery_voltage: float
    # Unknowns we haven't confirmed a mapping for:
    sensor1_mv: int                   # byte 13
    sensor2_mv: int                   # byte 15
    sensor3_mv: int                   # byte 16
    # Still unmapped:
    unknown_byte_3: int
    unknown_byte_5: int
    unknown_byte_11: int              # slowly-varying counter — likely CNS or GFs related
    unknown_byte_12: int              # 0x06 in SA-CCR, 0x00 in HW-CCR — mode/flags byte
    unknown_byte_19: int              # often mirrors byte 7 — possibly a second PPO2 reading
    tail: bytes                       # bytes 20..31 — tank/AI, event flags, CO2

    @property
    def ndl_min(self) -> int:
        return self.ndl_or_first_stop_min if self.first_stop_depth_m == 0 else 0

    @property
    def first_stop_time_min(self) -> int:
        return self.ndl_or_first_stop_min if self.first_stop_depth_m > 0 else 0

    @classmethod
    def from_block(cls, time_sec: int, block: bytes) -> "Sample":
        assert len(block) == BLOCK_SIZE and block[0] == 0x01
        depth_be = struct.unpack_from(">H", block, 1)[0]
        b3 = block[3]
        first_stop_m = block[4]
        b5 = block[5]
        tts = block[6]
        ppo2_cg = block[7]
        o2_pct = block[8]
        he_pct = block[9]
        ndl_or_fs = block[10]
        b11 = block[11]
        b12 = block[12]
        sensor1 = block[13]
        water_temp = block[14]
        sensor2 = block[15]
        sensor3 = block[16]
        # Battery voltage: bytes 17-18 BE u16, centivolts (handles both
        # 1.5 V AA on Petrel 3 and 4 V lithium on Tern).
        batt_cv = struct.unpack_from(">H", block, 17)[0]
        b19 = block[19]

        return cls(
            time_sec=time_sec,
            depth_m=depth_be / 10.0,
            first_stop_depth_m=first_stop_m,
            tts_min=tts,
            average_ppo2=ppo2_cg / 100.0,
            fraction_o2=o2_pct / 100.0,
            fraction_he=he_pct / 100.0,
            ndl_or_first_stop_min=ndl_or_fs,
            water_temp_c=water_temp,
            battery_voltage=batt_cv / 100.0,
            sensor1_mv=sensor1,
            sensor2_mv=sensor2,
            sensor3_mv=sensor3,
            unknown_byte_3=b3,
            unknown_byte_5=b5,
            unknown_byte_11=b11,
            unknown_byte_12=b12,
            unknown_byte_19=b19,
            tail=bytes(block[20:32]),
        )


@dataclass
class Event:
    """0x51 block — event/marker. Meaning not fully mapped yet."""
    offset: int
    raw: bytes


@dataclass
class TissueSnapshot:
    """A group of 0x70-0x75 blocks, each holding float32 tissue saturations.

    The 6 blocks contain a total of 48 little-endian float32 values after the
    4-byte tag/padding prefix, likely 16 N2 + 16 He compartments + extras.
    """
    offset: int
    floats: list[float]
    trailer: bytes  # last 8 bytes of the 0x75 block (contains dive-start epoch)


@dataclass
class HeaderBitmap:
    """0x80-0x87 / 0xa0-0xa1 block. 31 bytes of 0/1 flags after the tag."""
    tag: int
    bits: list[int]


@dataclass
class RawBlock:
    offset: int
    tag: int
    data: bytes


# Device family lives at bytes 10-11 (BE u16) of the 0x11 header block and
# is stable across dives on the same physical computer. Mode lives at byte
# 12 and varies per dive.
#
# The earlier revision of this parser read bytes 14-15 as a "product id";
# that turned out to be the GF setting (duplicated from h10[4:6]), which
# happened to match 0x1E50 / 0x2855 often enough to pass the original
# three-dive smoke test. Verified across 120 dives that bytes 10-11 give a
# stable per-device code and h11[14:16]BE == (h10[4]<<8 | h10[5]).
_DEVICE_FAMILIES = {
    0x1501: "Petrel 3",
    0x1400: "Tern",
}

# (device_family, mode_code) -> human-readable name. Add new entries as
# new logs are sighted.
_DEVICE_MODELS = {
    (0x1501, 0x0C): "Petrel 3 SA CCR",
    (0x1501, 0x09): "Petrel 3 HW JJCCR",
    (0x1400, 0x05): "Tern OC",
}


@dataclass
class DiveHeader:
    """Dive-level metadata extracted from the header (0x10-0x19) and footer
    (0x20-0x29) blocks.
    """
    dive_number: int
    serial_hex: str                       # e.g. "B2F10222"
    product_id: int                       # device family (block 0x11 bytes 10-11 BE)
    model_code: int                       # dive-mode code (block 0x11 byte 12)
    product_name: str                     # e.g. "Petrel 3 SA CCR", "Tern OC"
    firmware_build: str | None            # ASCII build id, e.g. "11301-10" if present
    firmware_version: tuple[int, int] | None  # (major, minor) from 0x18 block
    gf_min: int
    gf_max: int
    surface_interval_min: int             # minutes since previous dive ended
    dive_start_epoch: int
    dive_end_epoch: int | None
    max_depth_m: float                    # footer-reported max depth
    max_time_sec: int                     # footer-reported duration
    start_battery_v: float
    end_battery_v: float | None
    start_surface_pressure_mbar: int
    end_surface_pressure_mbar: int | None

    @property
    def dive_start_utc(self) -> str:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(self.dive_start_epoch, _dt.timezone.utc).isoformat()

    @property
    def dive_end_utc(self) -> str | None:
        if self.dive_end_epoch is None:
            return None
        import datetime as _dt
        return _dt.datetime.fromtimestamp(self.dive_end_epoch, _dt.timezone.utc).isoformat()


def _build_header(
    header_blocks: dict[int, bytes],
    footer_blocks: dict[int, bytes],
) -> DiveHeader | None:
    h10 = header_blocks.get(0x10)
    h11 = header_blocks.get(0x11)
    h14 = header_blocks.get(0x14)
    h18 = header_blocks.get(0x18)
    f20 = footer_blocks.get(0x20)
    f24 = footer_blocks.get(0x24)
    if not (h10 and h11):
        return None

    dive_number = h10[3]
    gf_min = h10[4]
    gf_max = h10[5]
    # Surface interval (minutes) — verified against firmware: header writer
    # at flash 0x080329a6 computes (BKP_SRAM[0x298] + 59) / 60 and stores
    # the result big-endian here, capped at 0xFFFF.
    surface_interval_min = struct.unpack_from(">H", h10, 6)[0]
    dive_start_epoch = struct.unpack_from(">I", h10, 12)[0]

    product_id = struct.unpack_from(">H", h11, 10)[0]
    model_code = h11[12]
    start_surface = struct.unpack_from(">H", h11, 16)[0]
    serial_hex = h11[18:22].hex().upper()

    start_batt = (struct.unpack_from(">H", h14, 5)[0] / 100.0) if h14 else 0.0

    firmware_build = None
    firmware_version = None
    if h18:
        # Petrel 3: ASCII build id at bytes 18..25, (major, minor) at 26-27.
        # Tern leaves this block mostly zero — firmware version location is
        # not yet mapped on Tern.
        if h18[26] or h18[27]:
            firmware_version = (h18[26], h18[27])
        try:
            build = h18[18:26].decode("ascii").strip("\x00")
            if build.isprintable() and build:
                firmware_build = build
        except UnicodeDecodeError:
            pass

    dive_end_epoch = None
    max_depth_m = 0.0
    max_time_sec = 0
    end_batt = None
    end_surface = None
    if f20:
        dive_end_epoch = struct.unpack_from(">I", f20, 12)[0]
        max_depth_m = struct.unpack_from(">H", f20, 4)[0] / 10.0
        max_time_sec = struct.unpack_from(">I", b"\x00" + f20[6:9])[0]
    f21 = footer_blocks.get(0x21)
    if f21:
        end_surface = struct.unpack_from(">H", f21, 16)[0] or None
    if f24:
        end_batt = struct.unpack_from(">H", f24, 5)[0] / 100.0

    product_name = _DEVICE_MODELS.get((product_id, model_code))
    if product_name is None:
        family = _DEVICE_FAMILIES.get(product_id, f"family=0x{product_id:04x}")
        product_name = f"{family} mode=0x{model_code:02x}"
    return DiveHeader(
        dive_number=dive_number,
        serial_hex=serial_hex,
        product_id=product_id,
        model_code=model_code,
        product_name=product_name,
        firmware_build=firmware_build,
        firmware_version=firmware_version,
        gf_min=gf_min,
        gf_max=gf_max,
        surface_interval_min=surface_interval_min,
        dive_start_epoch=dive_start_epoch,
        dive_end_epoch=dive_end_epoch,
        max_depth_m=max_depth_m,
        max_time_sec=max_time_sec,
        start_battery_v=start_batt,
        end_battery_v=end_batt,
        start_surface_pressure_mbar=start_surface,
        end_surface_pressure_mbar=end_surface,
    )


@dataclass
class DiveLog:
    dive_start_epoch: int | None
    header: DiveHeader | None = None
    samples: list[Sample] = field(default_factory=list)
    freedive_samples: list[RawBlock] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    tissues: list[TissueSnapshot] = field(default_factory=list)
    header_blocks: dict[int, bytes] = field(default_factory=dict)
    footer_blocks: dict[int, bytes] = field(default_factory=dict)
    bitmaps: list[HeaderBitmap] = field(default_factory=list)
    unknown: list[RawBlock] = field(default_factory=list)

    @property
    def max_depth_m(self) -> float:
        return max((s.depth_m for s in self.samples), default=0.0)

    @property
    def dive_duration_sec(self) -> int:
        return self.samples[-1].time_sec if self.samples else 0


def parse(path: str | Path) -> DiveLog:
    data = Path(path).read_bytes()
    if len(data) % BLOCK_SIZE != 0:
        raise ValueError(f"file length {len(data)} not a multiple of {BLOCK_SIZE}")

    log = DiveLog(dive_start_epoch=None)
    sample_index = 0
    pending_tissues: list[bytes] = []
    pending_tissue_offset = 0

    for off in range(0, len(data), BLOCK_SIZE):
        block = data[off:off + BLOCK_SIZE]
        tag = block[0]

        if 0x10 <= tag <= 0x1f:
            log.header_blocks[tag] = block
            if log.dive_start_epoch is None and tag == 0x10:
                # bytes 12..15 BE = dive-start unix time (matches log_id suffix)
                log.dive_start_epoch = struct.unpack_from(">I", block, 12)[0]
        elif 0x20 <= tag <= 0x2f:
            log.footer_blocks[tag] = block
        elif tag in (0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0xa0, 0xa1):
            log.bitmaps.append(HeaderBitmap(tag=tag, bits=list(block[1:])))
        elif tag == 0x01:
            log.samples.append(
                Sample.from_block(sample_index * SAMPLE_INTERVAL_SEC, block)
            )
            sample_index += 1
        elif tag == 0x02:
            log.freedive_samples.append(RawBlock(off, tag, block))
        elif tag == 0x30:
            # carries dive-start epoch at bytes 4..7 BE
            log.unknown.append(RawBlock(off, tag, block))
        elif tag == 0x51:
            log.events.append(Event(offset=off, raw=block))
        elif 0x70 <= tag <= 0x75:
            if tag == 0x70:
                pending_tissues = []
                pending_tissue_offset = off
            pending_tissues.append(block)
            if tag == 0x75 and len(pending_tissues) == 6:
                floats: list[float] = []
                for i, blk in enumerate(pending_tissues):
                    # Each tissue block: tag(1) + pad(3) + 7 float32 LE = 32 bytes.
                    # The 0x75 block ends with an 8-byte trailer (dive-start epoch).
                    count = 6 if i == 5 else 7
                    floats.extend(struct.unpack_from(f"<{count}f", blk, 4))
                trailer = pending_tissues[5][-8:]
                log.tissues.append(
                    TissueSnapshot(
                        offset=pending_tissue_offset,
                        floats=floats,
                        trailer=trailer,
                    )
                )
                pending_tissues = []
        elif tag in (0x00, 0xff):
            # padding / EOF
            log.unknown.append(RawBlock(off, tag, block))
        else:
            log.unknown.append(RawBlock(off, tag, block))

    log.header = _build_header(log.header_blocks, log.footer_blocks)
    return log


if __name__ == "__main__":
    import sys

    log = parse(sys.argv[1])
    h = log.header
    if h:
        print(f"product          = {h.product_name} (0x{h.product_id:04x})")
        print(f"serial           = {h.serial_hex}")
        print(f"firmware         = build={h.firmware_build} version={h.firmware_version}")
        print(f"dive number      = {h.dive_number}")
        print(f"gf               = {h.gf_min}/{h.gf_max}")
        print(f"surface interval = {h.surface_interval_min} min")
        print(f"dive start (UTC) = {h.dive_start_utc}  ({h.dive_start_epoch})")
        print(f"dive end   (UTC) = {h.dive_end_utc}  ({h.dive_end_epoch})")
        print(f"max depth        = {h.max_depth_m} m")
        print(f"max time         = {h.max_time_sec} s ({h.max_time_sec/60:.1f} min)")
        print(f"start battery    = {h.start_battery_v} V")
        print(f"end battery      = {h.end_battery_v} V")
        print(f"surface pressure = start {h.start_surface_pressure_mbar}  end {h.end_surface_pressure_mbar} mbar")
    print()
    print(f"samples          = {len(log.samples)}")
    print(f"freedive samples = {len(log.freedive_samples)}")
    print(f"events (0x51)    = {len(log.events)}")
    print(f"tissue snapshots = {len(log.tissues)}")
    print(f"bitmaps          = {len(log.bitmaps)}")
    print(f"header blocks    = {sorted(hex(t) for t in log.header_blocks)}")
    print(f"footer blocks    = {sorted(hex(t) for t in log.footer_blocks)}")
    print(f"max depth        = {log.max_depth_m} m")
    print(f"duration         = {log.dive_duration_sec} s ({log.dive_duration_sec/60:.1f} min)")
    print()
    print("first 3 samples:")
    for s in log.samples[:3]:
        print(f"  t={s.time_sec:4d}s  d={s.depth_m:4.1f}m  ppo2={s.average_ppo2:.2f}  "
              f"o2={s.fraction_o2:.2f}  he={s.fraction_he:.2f}  "
              f"ndl={s.ndl_min}  temp={s.water_temp_c}  batt={s.battery_voltage:.2f}")
