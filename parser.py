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


# Bit flags in the per-sample status byte (block[12]). Cross-referenced
# against libdivecomputer's shearwater_predator_parser.c (GASSWITCH /
# PPO2_EXTERNAL / SETPOINT_HIGH / SC / OC).
STATUS_GASSWITCH     = 0x01
STATUS_PPO2_EXTERNAL = 0x02
STATUS_SETPOINT_HIGH = 0x04
STATUS_SC            = 0x08
STATUS_OC            = 0x10


@dataclass
class Sample:
    time_sec: int
    depth_m: float
    first_stop_depth_m: int           # whole meters (block[4]; high byte in block[3] is 0 for all sighted dives)
    tts_min: int                      # minutes (block[6]; high byte in block[5] is 0 for all sighted dives)
    average_ppo2: float
    fraction_o2: float
    fraction_he: float
    ndl_or_first_stop_min: int        # NDL when first_stop_depth_m == 0, else first-stop time
    water_temp_c: int                 # signed; block[14]
    battery_voltage: float
    sensor1_mv: int                   # block[13]
    sensor2_mv: int                   # block[15]
    sensor3_mv: int                   # block[16]
    status: int                       # block[12] — CCR/OC/setpoint/gas-switch flags; see STATUS_* above
    setpoint: float                   # block[19] / 100 — CCR target PPO2 (bar)
    cns: float                        # block[23] / 100 — CNS oxygen toxicity %
    # Still unmapped — retained as a raw aperture for future decoding.
    unknown_byte_5: int               # high byte of tts when it exceeds 255 min
    unknown_byte_11: int              # slowly-varying; purpose unclear
    unknown_byte_22: int              # adjacent to cns, purpose unclear
    tail: bytes                       # bytes 20..31 — tank/AI pressure, gas-switch events, etc.

    @property
    def ndl_min(self) -> int:
        return self.ndl_or_first_stop_min if self.first_stop_depth_m == 0 else 0

    @property
    def first_stop_time_min(self) -> int:
        return self.ndl_or_first_stop_min if self.first_stop_depth_m > 0 else 0

    @property
    def dive_mode(self) -> str:
        """Decoded from the status byte.

        Matches libdivecomputer's logic:
        OC flag set -> open-circuit; else SC flag set -> semi-closed;
        otherwise closed-circuit.
        """
        if self.status & STATUS_OC:
            return "OC"
        if self.status & STATUS_SC:
            return "SC"
        return "CC"

    @property
    def is_gas_switch(self) -> bool:
        """True if this sample marks a gas switch (status bit 0)."""
        return bool(self.status & STATUS_GASSWITCH)

    @property
    def setpoint_is_high(self) -> bool:
        """True if the CCR high setpoint is active on this sample (vs low)."""
        return bool(self.status & STATUS_SETPOINT_HIGH)

    @property
    def has_external_ppo2(self) -> bool:
        """True if external O2 sensors are being used (CCR-only)."""
        return bool(self.status & STATUS_PPO2_EXTERNAL)

    @classmethod
    def from_block(cls, time_sec: int, block: bytes) -> "Sample":
        assert len(block) == BLOCK_SIZE and block[0] == 0x01
        depth_be = struct.unpack_from(">H", block, 1)[0]
        first_stop_m = block[4]
        tts = block[6]
        ppo2_cg = block[7]
        o2_pct = block[8]
        he_pct = block[9]
        ndl_or_fs = block[10]
        status = block[12]
        sensor1 = block[13]
        # Temperature is a signed 8-bit value (supports below-freezing ice
        # dives).  The b5-b22 "unknown" bytes are retained verbatim.
        water_temp = struct.unpack_from("b", block, 14)[0]
        sensor2 = block[15]
        sensor3 = block[16]
        # Battery voltage: bytes 17-18 BE u16, centivolts (handles both
        # 1.5 V AA on Petrel 3 and 4 V lithium on Tern).
        batt_cv = struct.unpack_from(">H", block, 17)[0]
        setpoint_cg = block[19]
        cns_cg = block[23]

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
            status=status,
            setpoint=setpoint_cg / 100.0,
            cns=cns_cg / 100.0,
            unknown_byte_5=block[5],
            unknown_byte_11=block[11],
            unknown_byte_22=block[22],
            tail=bytes(block[20:32]),
        )


@dataclass
class Event:
    """0x51 block — event/marker. Meaning not fully mapped yet."""
    offset: int
    raw: bytes


# 0x30 info-event subtype byte (`block[1]`) that carries a tag / bearing /
# bookmark. Matches libdivecomputer's INFO_EVENT_TAG_LOG.
INFO_EVENT_TAG_LOG = 38


@dataclass
class Bookmark:
    """A user-tagged bookmark event (0x30 block with type byte == 38).

    The user pressed the tag button on the dive computer at this point.
    If the compass was active, a bearing is recorded; otherwise
    `bearing_deg` is None. `raw_timestamp` is the u32 BE at bytes 4-7 of
    the 0x30 block — interpretation unclear (libdivecomputer marks it as
    unused; our earlier parser assumed it mirrors the dive-start epoch).
    """
    offset: int                      # byte offset of the 0x30 record
    bearing_deg: int | None          # 0..359 or None if compass was off (0xFFFFFFFF)
    tag_value: int                   # u32 BE at block[12..15] — user-assigned tag id
    raw_timestamp: int               # u32 BE at block[4..7]


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
    0x1501: "Petrel/Perdix",  # shared family code for Petrel 3, Perdix 2,
                              # and likely older Petrel/Perdix variants
    0x1400: "Tern",
}

# (device_family, mode_code) -> human-readable name. Add new entries as
# new logs are sighted.
_DEVICE_MODELS = {
    (0x1501, 0x0C): "Petrel 3 SA CCR",
    (0x1501, 0x09): "Petrel 3 HW JJCCR",
    (0x1501, 0x03): "Perdix 2 SA CCR",
    (0x1400, 0x05): "Tern OC",
}


# Dive-mode enum at block 0x14 byte 1 (for PNF). Indices and labels mirror
# libdivecomputer's shearwater_predator_parser.c.
_DIVE_MODE_LABELS = [
    "CC / BO",          # 0  M_CC
    "OC Technical",     # 1  M_OC_TEC
    "Gauge",            # 2  M_GAUGE
    "PPO2 Display",     # 3  M_PPO2
    "SC / BO",          # 4  M_SC
    "CC / BO 2",        # 5  M_CC2
    "OC Recreational",  # 6  M_OC_REC
    "Freedive",         # 7  M_FREEDIVE
]

_BATTERY_TYPES = {
    1: "1.5V Alkaline",
    2: "1.5V Lithium",
    3: "1.2V NiMH",
    4: "3.6V Saft",
    5: "3.7V Li-Ion",
}

# Deco model enum at block 0x12 byte 18 (for PNF).
_DECO_MODEL_NAMES = {
    0: "GF",
    1: "VPM-B",
    2: "VPM-B/GFS",
}


@dataclass
class GasMix:
    """A gas mix as used on a dive.

    Built by scanning the sample stream for unique (o2, he, ccr) triples —
    the Petrel Native Format does not store a fixed gas-mix table in the
    header. Matches libdivecomputer's approach in
    shearwater_predator_parser.c (sample-driven gasmix accumulation).
    """
    fraction_o2: float        # 0.0 .. 1.0
    fraction_he: float        # 0.0 .. 1.0
    is_diluent: bool          # True when the gas was active during closed-circuit
    first_seen_sec: int       # time (s) of the first sample to carry this mix


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
    # Added from libdivecomputer-cross-referenced byte offsets:
    units: str                            # "metric" or "imperial" (h10[8])
    salinity_g_per_l: int                 # water density; 1000 = fresh, ~1020 = salt (h13[3..4] BE)
    dive_mode_str: str                    # e.g. "CC / BO", "OC Technical", "Freedive" (h14[1])
    battery_type: str | None              # e.g. "1.5V Alkaline" (h14[9]); None if not encoded
    deco_model: str                       # e.g. "GF 30/80", "VPM-B +3", "VPM-B/GFS +3 80%"
    utc_offset_min: int                   # signed; UTC offset in minutes (h15[26..29] BE i32).
                                          # 0 in standalone .swlogdata exports — Cloud's exporter
                                          # appears to normalize timestamps to UTC and zero this
                                          # out. Still carried for any exports that preserve it.
    is_dst: bool                          # whether DST is in effect at dive time (h15[30])
    log_version: int                      # Shearwater log-format version (h14[16]); features are
                                          # gated on this (e.g. CCR stack time requires >= 11).
    ccr_stack_total_sec: int | None       # configured scrubber lifetime in seconds (h16[1..2] BE).
                                          # None when log_version < 11 or stack time isn't set.
    ccr_stack_remaining_start_sec: int | None  # remaining scrubber time at dive start (h16[3..4] BE)
    ccr_stack_remaining_end_sec: int | None    # remaining scrubber time at dive end (f26[3..4] BE)
    sensor_calibration_bars_per_mv: tuple[float | None, float | None, float | None]
    """Per-O2-sensor calibration (bar / mV). None for an uncalibrated sensor.
    libdivecomputer uses these to scale raw sensor mV into PPO2."""

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
    h12 = header_blocks.get(0x12)
    h13 = header_blocks.get(0x13)
    h14 = header_blocks.get(0x14)
    h15 = header_blocks.get(0x15)
    h16 = header_blocks.get(0x16)
    h18 = header_blocks.get(0x18)
    f20 = footer_blocks.get(0x20)
    f24 = footer_blocks.get(0x24)
    f26 = footer_blocks.get(0x26)
    if not (h10 and h11):
        return None

    dive_number = h10[3]
    gf_min = h10[4]
    gf_max = h10[5]
    units = "imperial" if h10[8] == 1 else "metric"
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

    # Salinity / water density (h13 bytes 3..4 BE) — 1000 = fresh water,
    # ~1020 = salt. Absent block → default to 1000 (fresh) like libdc.
    salinity = struct.unpack_from(">H", h13, 3)[0] if h13 else 1000

    # Dive mode enum (h14 byte 1): 0..7 maps to labels above.
    dive_mode_str = _DIVE_MODE_LABELS[h14[1]] if h14 and h14[1] < len(_DIVE_MODE_LABELS) else (
        f"Unknown ({h14[1]})" if h14 else "Unknown"
    )

    # Battery type (h14 byte 9). Unsighted (zero) → leave as None rather than
    # claiming "unknown type 0".
    battery_type: str | None = None
    if h14 and h14[9]:
        battery_type = _BATTERY_TYPES.get(h14[9], f"unknown type {h14[9]}")

    # Deco model: enum byte at h12[18], with param at h12[19] (VPM-B
    # conservatism) and h13[5] (GFS percentage for VPM-B/GFS).
    if h12 is None:
        deco_model = f"GF {gf_min}/{gf_max}"  # safe fallback
    else:
        model = h12[18]
        if model == 0:
            deco_model = f"GF {gf_min}/{gf_max}"
        elif model == 1:
            deco_model = f"VPM-B +{h12[19]}"
        elif model == 2:
            gfs = h13[5] if h13 else 0
            deco_model = f"VPM-B/GFS +{h12[19]} {gfs}%"
        else:
            deco_model = _DECO_MODEL_NAMES.get(model, f"Unknown model {model}")

    # UTC offset (minutes, signed) and DST flag (h15[26..30]). libdivecomputer
    # stores this raw as a BE i32. Empirically on Shearwater the unit is
    # minutes: Tern dive at IST shows 120 = 2 hours, matching libdc's Subsurface
    # consumers that multiply by 60 to get seconds.
    if h15:
        utc_offset_min = struct.unpack_from(">i", h15, 26)[0]
        is_dst = bool(h15[30])
    else:
        utc_offset_min = 0
        is_dst = False

    # Sensor calibration (h13): bitmap at byte 6, three BE u16 values at
    # bytes 7..12. Raw value is (mV per bar PPO2) × 100000, so divide by
    # 100000 to get bar per mV — the inverse of the ratio libdc caches.
    calib: list[float | None] = [None, None, None]
    if h13:
        bitmap = h13[6]
        for i in range(3):
            if bitmap & (1 << i):
                raw = struct.unpack_from(">H", h13, 7 + i * 2)[0]
                calib[i] = raw / 100000.0
    sensor_calibration = (calib[0], calib[1], calib[2])

    # Log version (h14[16]). libdivecomputer gates a handful of features
    # on this (AI layout, tank naming, stack time, …).
    log_version = h14[16] if h14 else 0

    # CCR scrubber stack time (log_version >= 11). Values are seconds.
    # A total of 0 means stack-time tracking is disabled; in that case
    # libdc skips the remaining fields, so we do the same.
    ccr_stack_total = None
    ccr_stack_remaining_start = None
    ccr_stack_remaining_end = None
    if h16 and log_version >= 11:
        total = struct.unpack_from(">H", h16, 1)[0]
        if total > 0:
            ccr_stack_total = total
            ccr_stack_remaining_start = struct.unpack_from(">H", h16, 3)[0]
            if f26:
                ccr_stack_remaining_end = struct.unpack_from(">H", f26, 3)[0]

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
        units=units,
        salinity_g_per_l=salinity,
        dive_mode_str=dive_mode_str,
        battery_type=battery_type,
        deco_model=deco_model,
        utc_offset_min=utc_offset_min,
        is_dst=is_dst,
        sensor_calibration_bars_per_mv=sensor_calibration,
        log_version=log_version,
        ccr_stack_total_sec=ccr_stack_total,
        ccr_stack_remaining_start_sec=ccr_stack_remaining_start,
        ccr_stack_remaining_end_sec=ccr_stack_remaining_end,
    )


def _build_gas_mixes(samples: list[Sample]) -> list[GasMix]:
    """Extract the unique gas mixes used on this dive by scanning samples.

    Mirrors libdivecomputer's sample-driven gasmix accumulation: whenever a
    new (o2, he, is_diluent) triple appears that hasn't been seen before, it
    is appended to the list. Sample index -> time (s) at 10 s cadence.
    """
    seen: list[GasMix] = []
    for s in samples:
        # Skip the synthetic "no gas" sample (surface, before first breath).
        if s.fraction_o2 == 0 and s.fraction_he == 0:
            continue
        is_diluent = not (s.status & STATUS_OC)
        key = (round(s.fraction_o2, 3), round(s.fraction_he, 3), is_diluent)
        if any(
            (round(m.fraction_o2, 3), round(m.fraction_he, 3), m.is_diluent) == key
            for m in seen
        ):
            continue
        seen.append(
            GasMix(
                fraction_o2=s.fraction_o2,
                fraction_he=s.fraction_he,
                is_diluent=is_diluent,
                first_seen_sec=s.time_sec,
            )
        )
    return seen


@dataclass
class DiveLog:
    dive_start_epoch: int | None
    header: DiveHeader | None = None
    samples: list[Sample] = field(default_factory=list)
    freedive_samples: list[RawBlock] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    tissues: list[TissueSnapshot] = field(default_factory=list)
    gas_mixes: list[GasMix] = field(default_factory=list)
    bookmarks: list[Bookmark] = field(default_factory=list)
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
            # Info-event record. libdivecomputer handles one subtype
            # (INFO_EVENT_TAG_LOG = 38 = tagged bookmark with optional
            # compass bearing); other subtypes pass through unparsed.
            if block[1] == INFO_EVENT_TAG_LOG:
                bearing_raw = struct.unpack_from(">I", block, 8)[0]
                tag_value = struct.unpack_from(">I", block, 12)[0]
                timestamp = struct.unpack_from(">I", block, 4)[0]
                log.bookmarks.append(
                    Bookmark(
                        offset=off,
                        bearing_deg=None if bearing_raw == 0xFFFFFFFF else bearing_raw,
                        tag_value=tag_value,
                        raw_timestamp=timestamp,
                    )
                )
            else:
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
    log.gas_mixes = _build_gas_mixes(log.samples)
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
