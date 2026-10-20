"""Decode SPAN SBX (RS-485) frames with variable-length protobuf payloads.

Wire layout (logical bytes after unescape; SOF/EOF are raw on the wire):

  [ 0x60 | Ver(1) | RcvrID(2) | SndrID(2) | HdrCRC(2)
        | TxnID(1) | MsgTypeID(2) | PayloadLen(1)
        | Payload(N) | PayloadCRC(2) | 0x62 ]

Payload length N is taken from PayloadLen — not a fixed frame size.
Bytes 0x60 / 0x62 / 0x5E inside the escaped body are sent as
``0x5E`` + ``(byte - 1)``.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

PREAMBLE = 0x60
POSTAMBLE = 0x62
ESCAPE = 0x5E
ESCAPE_ADJUST = 1

# MsgTypeID names from "Sbx decode" sheet + msgid.proto
MSG_TYPES: dict[int, str] = {
    0x0000: "NONE",
    0x0001: "MsgHeartbeatRequest",
    0x0002: "MsgResetRequest",
    0x0003: "MsgProtocolVersionRequest",
    0x0004: "MsgProtocolVersion",
    0x0005: "MsgFirmwareVersionRequest",
    0x0006: "MsgFirmwareVersion",
    0x0007: "MsgDeviceInfoRequest",
    0x0008: "MsgDeviceInfo",
    0x0009: "MsgLogRequest",
    0x000A: "MsgLog",
    0x000B: "MsgThreadStateRequest",
    0x000C: "MsgThreadState",
    0x000D: "MsgCpuUtilRequest",
    0x000E: "MsgCpuUtil",
    0x000F: "MsgBootloaderVersionRequest",
    0x0010: "MsgBootloaderVersion",
    0x0011: "MsgCommStatusRequest",
    0x0012: "MsgCommStatus",
    0x0013: "MsgGoldenOrActiveImgVersionRequest",
    0x0014: "MsgGoldenOrActiveImgVersion",
    0x0015: "MsgExtFlashImgVersionRequest",
    0x0016: "MsgExtFlashImgVersion",
    0x1000: "MsgDriveStatusRequest",
    0x1001: "MsgDriveStatus",
    0x1002: "MsgDriveTempRequest",
    0x1003: "MsgDriveTemp",
    0x1004: "MsgDriveInstCurrentLimitRequest",
    0x1005: "MsgDriveInstCurrentLimit",
    0x1006: "MsgDriveHwCurrentLimitRequest",
    0x1007: "MsgDriveHwCurrentLimit",
    0x100A: "MsgSetChargeRate",
    0x100B: "MsgDriveFaultCountRequest",
    0x100C: "MsgFaultCount",
    0x100F: "MsgDriveStartupRequest",
    0x1010: "MsgDriveStartup",
    0x2000: "MsgAssignDynamicBusId",
    0x2001: "MsgDiscoveryState",
    0x3000: "MsgFileWrite",
    0x3001: "MsgSoftwareUpdate",
    0x3002: "MsgSoftwareUpdateStatus",
    0x3003: "MsgFileWriteComplete",
    0x3004: "MsgFileRemove",
    0x3005: "MsgFileReadRequest",
    0x3006: "MsgFileReadResponse",
    0x3007: "MsgFileReadComplete",
    0x3008: "MsgFileSizeRequest",
    0x3009: "MsgFileSizeResponse",
    0x4000: "MsgSettingWriteRequest",
    0x4001: "MsgSettingReadRequest",
    0x4002: "MsgSettingResponse",
    0x5000: "MsgStpmRegisterRequest",
    0x5001: "MsgStpmRegister",
    0x5002: "MsgStpmResetStatusReg",
    0xF000: "MsgAck",
    0xFFFF: "ALL",
}

CHARGING_STATE = {
    0: "UNKNOWN",
    1: "A_DISCONNECTED",
    2: "B1_CONNECTED",
    3: "B2_READY",
    4: "C_CHARGING",
    5: "D_VENT_REQ",
    6: "E_ERROR",
    7: "F_FAULT",
}

HEADER_LOGICAL_LEN = 11  # ver..payload_len inclusive
_HEX_TOKEN = re.compile(r"[0-9A-Fa-f]{2}")


def crc16_ccitt(data: bytes, crc: int = 0) -> int:
    """CRC-16/CCITT-FALSE matching libsbx ``crc16_ccitt``."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def parse_hex_bytes(text: str) -> bytes:
    """Parse space/comma/newline-separated hex into bytes (ignores non-hex)."""
    tokens = _HEX_TOKEN.findall(text)
    return bytes(int(t, 16) for t in tokens)


@dataclass
class SbxFrame:
    version: int
    receiver_id: int
    sender_id: int
    header_crc: int
    header_crc_ok: bool
    transaction_id: int
    msg_type: int
    payload_len: int
    payload: bytes
    payload_crc: int
    payload_crc_ok: bool
    raw_wire: bytes
    decoded: dict[str, Any] = field(default_factory=dict)

    @property
    def msg_name(self) -> str:
        return MSG_TYPES.get(self.msg_type, f"UNKNOWN_0x{self.msg_type:04X}")

    @property
    def direction(self) -> str:
        """Best-effort Panel↔CP label from MsgTypeID (sheet semantics)."""
        mt = self.msg_type
        if mt == 0xF000:
            return "CP → Panel (ACK)"
        # Responses / CP→Panel message IDs
        if mt in (
            0x0006,
            0x0008,
            0x000A,
            0x000C,
            0x000E,
            0x0010,
            0x0012,
            0x0014,
            0x0016,
            0x1001,
            0x1003,
            0x1005,
            0x1007,
            0x100C,
            0x1010,
            0x2001,
            0x3006,
            0x3009,
            0x4002,
            0x5001,
        ):
            return "CP → Panel"
        # Empty / request-style IDs → Panel→CP
        if mt in MSG_TYPES and mt < 0xF000:
            return "Panel → CP"
        return f"0x{self.sender_id:04X} → 0x{self.receiver_id:04X}"

    def summary_line(self) -> str:
        bits = [
            f"{self.direction}",
            f"{self.msg_name} (0x{self.msg_type:04X})",
            f"len={self.payload_len}",
            f"txn={self.transaction_id}",
            f"src=0x{self.sender_id:04X}",
            f"dst=0x{self.receiver_id:04X}",
        ]
        if self.decoded:
            interesting = {
                k: v
                for k, v in self.decoded.items()
                if not k.startswith("_")
            }
            if interesting:
                kv = ", ".join(f"{k}={v}" for k, v in interesting.items())
                bits.append(kv)
        return " | ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "receiver_id": f"0x{self.receiver_id:04X}",
            "sender_id": f"0x{self.sender_id:04X}",
            "header_crc": f"0x{self.header_crc:04X}",
            "header_crc_ok": self.header_crc_ok,
            "transaction_id": self.transaction_id,
            "msg_type": f"0x{self.msg_type:04X}",
            "msg_name": self.msg_name,
            "payload_len": self.payload_len,
            "payload_hex": self.payload.hex(" "),
            "payload_crc": f"0x{self.payload_crc:04X}",
            "payload_crc_ok": self.payload_crc_ok,
            "direction": self.direction,
            "decoded": self.decoded,
            "raw_wire_hex": self.raw_wire.hex(" "),
            "summary": self.summary_line(),
        }


def _read_escaped(wire: bytes, pos: int, n_logical: int) -> tuple[Optional[bytearray], int]:
    """Read ``n_logical`` unescaped bytes starting at ``pos``. Returns (None, pos) if short."""
    out = bytearray()
    i = pos
    while len(out) < n_logical:
        if i >= len(wire):
            return None, pos
        b = wire[i]
        i += 1
        if b == ESCAPE:
            if i >= len(wire):
                return None, pos
            out.append((wire[i] + ESCAPE_ADJUST) & 0xFF)
            i += 1
        else:
            # Raw SOF/EOF mid-body means framing error — abort this attempt
            if b in (PREAMBLE, POSTAMBLE):
                return None, pos
            out.append(b)
    return out, i


def try_parse_frame_at(wire: bytes, start: int) -> tuple[Optional[SbxFrame], int]:
    """
    Attempt to parse one SBX frame starting at ``wire[start]`` (must be PREAMBLE).

    Returns ``(frame, next_index)`` on success, or ``(None, resync_index)``.
    ``resync_index`` is ``start+1`` on hard failure, or ``start`` if more bytes needed.
    """
    if start >= len(wire) or wire[start] != PREAMBLE:
        return None, start + 1

    header, after_hdr = _read_escaped(wire, start + 1, HEADER_LOGICAL_LEN)
    if header is None:
        # Incomplete — wait for more data unless a new SOF appears soon
        return None, start

    version = header[0]
    receiver_id = struct.unpack_from("<H", header, 1)[0]
    sender_id = struct.unpack_from("<H", header, 3)[0]
    header_crc = struct.unpack_from("<H", header, 5)[0]
    transaction_id = header[7]
    msg_type = struct.unpack_from("<H", header, 8)[0]
    payload_len = header[10]

    expected_hdr_crc = crc16_ccitt(bytes(header[0:5]))
    header_crc_ok = expected_hdr_crc == header_crc

    body, after_body = _read_escaped(wire, after_hdr, payload_len + 2)
    if body is None:
        return None, start

    if after_body >= len(wire):
        return None, start
    if wire[after_body] != POSTAMBLE:
        # Bad frame — skip this SOF
        return None, start + 1

    payload = bytes(body[:payload_len])
    payload_crc = struct.unpack_from("<H", body, payload_len)[0]
    expected_pl_crc = crc16_ccitt(
        bytes([transaction_id])
        + struct.pack("<H", msg_type)
        + bytes([payload_len])
        + payload
    )
    # Payload CRC covers txn + msg_type + len + payload (see libsbx send_payload)
    payload_crc_ok = expected_pl_crc == payload_crc

    end = after_body + 1
    frame = SbxFrame(
        version=version,
        receiver_id=receiver_id,
        sender_id=sender_id,
        header_crc=header_crc,
        header_crc_ok=header_crc_ok,
        transaction_id=transaction_id,
        msg_type=msg_type,
        payload_len=payload_len,
        payload=payload,
        payload_crc=payload_crc,
        payload_crc_ok=payload_crc_ok,
        raw_wire=bytes(wire[start:end]),
    )
    frame.decoded = decode_payload(msg_type, payload)
    return frame, end


@dataclass
class SbxJunkChunk:
    """Raw bytes discarded by the SBX framer (no valid frame)."""

    raw: bytes
    reason: str

    def summary_line(self) -> str:
        labels = {
            "no_sof_prefix": "UNDECODED (bytes before 0x60 SOF)",
            "parse_failed": "UNDECODED (invalid frame — resync)",
            "no_sof_in_buffer": "UNDECODED (no SOF in buffer)",
            "trailing_incomplete": "UNDECODED (incomplete at stop)",
        }
        label = labels.get(self.reason, f"UNDECODED ({self.reason})")
        n = len(self.raw)
        hex_part = self.raw.hex(" ") if self.raw else "(empty)"
        return f"{label} | {n} byte(s) | {hex_part}"


class SbxStreamFramer:
    """Incremental framer for continuous SBX byte streams (variable PayloadLen)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> tuple[list[SbxFrame], list[SbxJunkChunk]]:
        """Return decoded frames plus any bytes that could not be framed."""
        self._buf.extend(chunk)
        frames: list[SbxFrame] = []
        junk_out: list[SbxJunkChunk] = []
        while True:
            try:
                sof = self._buf.index(PREAMBLE)
            except ValueError:
                if self._buf:
                    junk_out.append(
                        SbxJunkChunk(bytes(self._buf), "no_sof_in_buffer")
                    )
                    self._buf.clear()
                break
            if sof:
                junk_out.append(SbxJunkChunk(bytes(self._buf[:sof]), "no_sof_prefix"))
                del self._buf[:sof]

            frame, nxt = try_parse_frame_at(self._buf, 0)
            if frame is not None:
                frames.append(frame)
                del self._buf[:nxt]
                continue
            if nxt == 0:
                break
            junk_out.append(SbxJunkChunk(bytes(self._buf[:nxt]), "parse_failed"))
            del self._buf[:nxt]
        return frames, junk_out

    def flush_remaining(self) -> Optional[SbxJunkChunk]:
        """Return trailing bytes that never formed a complete frame."""
        if not self._buf:
            return None
        junk = SbxJunkChunk(bytes(self._buf), "trailing_incomplete")
        self._buf.clear()
        return junk


def extract_frames(data: bytes) -> list[SbxFrame]:
    framer = SbxStreamFramer()
    frames, _junk = framer.feed(data)
    return frames


def decode_varint(data: bytes, pos: int) -> tuple[Optional[int], int]:
    result = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            return None, pos
    return None, pos


def iter_protobuf_fields(data: bytes) -> Iterator[tuple[int, int, Any]]:
    """Yield ``(field_number, wire_type, value)`` for each protobuf field."""
    pos = 0
    while pos < len(data):
        tag, new_pos = decode_varint(data, pos)
        if tag is None:
            break
        field_num = tag >> 3
        wire_type = tag & 0x07
        pos = new_pos
        if wire_type == 0:  # varint
            value, pos = decode_varint(data, pos)
            if value is None:
                break
            yield field_num, wire_type, value
        elif wire_type == 2:  # length-delimited
            length, pos = decode_varint(data, pos)
            if length is None or pos + length > len(data):
                break
            yield field_num, wire_type, data[pos : pos + length]
            pos += length
        elif wire_type == 5:  # 32-bit
            if pos + 4 > len(data):
                break
            yield field_num, wire_type, struct.unpack_from("<I", data, pos)[0]
            pos += 4
        elif wire_type == 1:  # 64-bit
            if pos + 8 > len(data):
                break
            yield field_num, wire_type, struct.unpack_from("<Q", data, pos)[0]
            pos += 8
        else:
            break


def decode_payload(msg_type: int, payload: bytes) -> dict[str, Any]:
    if not payload:
        return dict()

    if msg_type == 0xF000:
        return _decode_ack(payload)
    if msg_type == 0x1001:
        return _decode_drive_status(payload)
    if msg_type == 0x100A:
        return _decode_set_charge_rate(payload)
    if msg_type == 0x0006:
        return _decode_firmware_version(payload)
    if msg_type == 0x0012:
        return _decode_comm_status(payload)
    if msg_type == 0x1010:
        return _decode_drive_startup(payload)
    if msg_type == 0x2000:
        return _decode_assign_bus_id(payload)

    # Generic field dump for unknown / less critical types
    fields = dict()
    for num, wtype, value in iter_protobuf_fields(payload):
        key = f"field_{num}"
        if wtype == 2 and isinstance(value, (bytes, bytearray)):
            try:
                fields[key] = value.decode("utf-8")
            except UnicodeDecodeError:
                fields[key] = value.hex(" ")
        else:
            fields[key] = value
    fields["_raw_hex"] = payload.hex(" ")
    return fields


def _fields_map(payload: bytes) -> dict[int, Any]:
    out: dict[int, Any] = dict()
    for num, _wtype, value in iter_protobuf_fields(payload):
        # repeated fields: keep last (good enough for status) or list
        if num in out:
            prev = out[num]
            if isinstance(prev, list):
                prev.append(value)
            else:
                out[num] = [prev, value]
        else:
            out[num] = value
    return out


def _decode_ack(payload: bytes) -> dict[str, Any]:
    fields = _fields_map(payload)
    status_map = {0: "OK", 1: "FAILED", 2: "ERROR_PAYLOAD_CRC"}
    result: dict[str, Any] = {"_proto": "MsgAck"}
    if 1 in fields:
        result["command_status"] = status_map.get(int(fields[1]), f"UNKNOWN_{fields[1]}")
    if 2 in fields:
        result["error_code"] = fields[2]
    return result


def _decode_set_charge_rate(payload: bytes) -> dict[str, Any]:
    fields = _fields_map(payload)
    result: dict[str, Any] = {"_proto": "MsgSetChargeRate"}
    if 1 in fields:
        result["charge_rate_a"] = fields[1]
    return result


def _decode_firmware_version(payload: bytes) -> dict[str, Any]:
    fields = _fields_map(payload)
    result: dict[str, Any] = {"_proto": "MsgFirmwareVersion"}
    for num, name in (
        (1, "major"),
        (2, "minor"),
        (3, "build"),
        (5, "image_crc"),
        (6, "header_crc"),
        (7, "current_image"),
    ):
        if num in fields:
            result[name] = fields[num]
    if 4 in fields and isinstance(fields[4], (bytes, bytearray)):
        result["git_sha"] = fields[4].decode("utf-8", errors="replace")
    return result


def _decode_comm_status(payload: bytes) -> dict[str, Any]:
    fields = _fields_map(payload)
    names = {
        1: "msg_sent_cnt",
        2: "msg_received_cnt",
        3: "bad_crc_cnt",
        4: "framing_err_cnt",
        5: "usart_io_err_cnt",
        6: "rx_buffer_util",
    }
    result: dict[str, Any] = {"_proto": "MsgCommStatus"}
    for num, name in names.items():
        if num in fields:
            result[name] = fields[num]
    return result


def _decode_drive_startup(payload: bytes) -> dict[str, Any]:
    fields = _fields_map(payload)
    names = {
        1: "boot_counter",
        2: "reset_cause",
        3: "fault_code",
        4: "watchdog_thread",
        5: "image",
        6: "uptime",
    }
    result: dict[str, Any] = {"_proto": "MsgDriveStartup"}
    for num, name in names.items():
        if num in fields:
            result[name] = fields[num]
    return result


def _decode_assign_bus_id(payload: bytes) -> dict[str, Any]:
    fields = _fields_map(payload)
    result: dict[str, Any] = {"_proto": "MsgAssignDynamicBusId"}
    if 1 in fields and isinstance(fields[1], (bytes, bytearray)):
        result["serial_number"] = fields[1].decode("utf-8", errors="replace")
    if 2 in fields:
        result["bus_id"] = fields[2]
    return result


def _decode_drive_status(payload: bytes) -> dict[str, Any]:
    """MsgDriveStatus — fields used by panel throttle / evidence."""
    fields = _fields_map(payload)
    result: dict[str, Any] = {"_proto": "MsgDriveStatus"}

    def _u(num: int, name: str, fmt=lambda x: x) -> None:
        if num in fields and not isinstance(fields[num], list):
            result[name] = fmt(int(fields[num]))

    if 1 in fields and not isinstance(fields[1], list):
        cs = int(fields[1])
        result["charging_state"] = CHARGING_STATE.get(cs, f"UNKNOWN_{cs}")
    _u(2, "fault_mask", lambda x: f"0x{x:08X}")
    _u(3, "charge_rate_a")
    _u(4, "observed_charge_rate_a")
    _u(5, "rotary_max_charge_rate_a")
    _u(6, "temp_line1_c")
    _u(7, "temp_line2_c")
    _u(8, "temp_amb_c")
    _u(9, "temp_throttle_a")
    _u(10, "rotary_mode")
    _u(11, "led_state")
    _u(13, "rms_voltage_l1_mv")
    _u(14, "rms_voltage_l2_mv")
    _u(15, "rms_current_l1_ma")
    _u(16, "rms_current_l2_ma")
    _u(17, "energy_acc_l1_wh")
    _u(18, "energy_acc_l2_wh")
    _u(20, "latch_mask", lambda x: f"0x{x:08X}")
    if 21 in fields and not isinstance(fields[21], list):
        result["lock_state"] = "LOCKED" if fields[21] else "UNLOCKED"
    _u(22, "requested_charge_rate_a")
    return result


def format_frame_block(frame: SbxFrame, index: int = 0) -> str:
    lines = [
        f"=== Frame {index} ===",
        f"DIRECTION : {frame.direction}",
        f"MSG       : {frame.msg_name} (0x{frame.msg_type:04X})",
        f"HEADER    : ver={frame.version} dst=0x{frame.receiver_id:04X} "
        f"src=0x{frame.sender_id:04X} hdr_crc=0x{frame.header_crc:04X}"
        f"{' OK' if frame.header_crc_ok else ' BAD'}",
        f"PAYLOAD   : txn={frame.transaction_id} len={frame.payload_len} "
        f"pl_crc=0x{frame.payload_crc:04X}"
        f"{' OK' if frame.payload_crc_ok else ' BAD'}",
    ]
    if frame.payload:
        lines.append(f"  raw     : {frame.payload.hex(' ')}")
    for key, value in frame.decoded.items():
        if key.startswith("_") and key != "_proto":
            continue
        lines.append(f"  {key}: {value}")
    lines.append(f"RAW WIRE  : {frame.raw_wire.hex(' ')}")
    return "\n".join(lines)


def decode_hex_text(text: str) -> list[SbxFrame]:
    return extract_frames(parse_hex_bytes(text))


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decode SPAN Drive SBX hex dumps (variable PayloadLen)."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Hex dump file, or '-' for stdin",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Write decoded text here (default: stdout)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per frame",
    )
    args = parser.parse_args(argv)

    if args.input == "-":
        text = sys.stdin.read()
    else:
        with open(args.input, encoding="utf-8") as fh:
            text = fh.read()

    frames = decode_hex_text(text)
    if args.json:
        import json

        body = "\n".join(json.dumps(f.to_dict()) for f in frames) + ("\n" if frames else "")
    else:
        blocks = [format_frame_block(f, i + 1) for i, f in enumerate(frames)]
        blocks.append(f"=== Summary: {len(frames)} frame(s) ===")
        body = "\n\n".join(blocks) + "\n"

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(body)
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
