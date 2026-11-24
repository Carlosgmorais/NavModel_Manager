"""Bounded Navisworks zlib member extraction.

This is a safe container helper, NOT a geometry decoder. NWC/NWD geometry must
be interpreted through verified records and transforms, never by float scanning.
"""
from __future__ import annotations
import zlib

_HEADERS = (b"\x78\x01", b"\x78\x9c", b"\x78\xda")


def iter_zlib_members(fileobj, offset: int, length: int, *,
                      read_size: int = 65536,
                      output_chunk_size: int = 65536,
                      max_total_output: int = 1024 * 1024 * 1024,
                      max_members: int = 20000):
    """Yield decompressed pieces without loading compressed stream into RAM.

    Scans for zlib members within a bounded stream. Raises on truncated streams,
    invalid members, oversized expanded data, or too many members.
    """
    if offset < 0 or length < 0 or read_size < 1024 or output_chunk_size < 1024:
        raise ValueError("Invalid Navisworks stream bounds")
    fileobj.seek(offset)
    remaining = length
    buffer = b""
    decoder = None
    members = 0
    total = 0

    while remaining > 0 or buffer:
        if decoder is None:
            # Fill conservatively; search may cross one read boundary.
            if len(buffer) < 2 and remaining:
                incoming = fileobj.read(min(read_size, remaining))
                if not incoming:
                    raise ValueError("Truncated Navisworks stream")
                remaining -= len(incoming)
                buffer += incoming

            positions = [buffer.find(h) for h in _HEADERS]
            positions = [p for p in positions if p >= 0]
            if not positions:
                buffer = buffer[-1:]  # preserve possible first header byte
                if not remaining:
                    break
                continue
            pos = min(positions)
            buffer = buffer[pos:]
            decoder = zlib.decompressobj()
            members += 1
            if members > max_members:
                raise ValueError("Too many Navisworks compressed members")

        if not buffer:
            if not remaining:
                raise ValueError("Truncated zlib member in Navisworks stream")
            incoming = fileobj.read(min(read_size, remaining))
            if not incoming:
                raise ValueError("Truncated Navisworks input")
            remaining -= len(incoming)
            buffer = incoming

        try:
            out = decoder.decompress(buffer, output_chunk_size)
        except zlib.error as exc:
            raise ValueError(f"Invalid Navisworks compressed member: {exc}") from exc

        if decoder.eof:
            buffer = decoder.unused_data
            decoder = None
        else:
            buffer = decoder.unconsumed_tail

        if out:
            total += len(out)
            if total > max_total_output:
                raise ValueError("Navisworks expanded-data budget exceeded")
            yield out

    if decoder is not None:
        raise ValueError("Truncated zlib member in Navisworks stream")
