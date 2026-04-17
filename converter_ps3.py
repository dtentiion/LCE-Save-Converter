"""
LCE Save Converter - PS3 Backend
Converts PS3 Minecraft save folders (GAMEDATA) to Windows64 LCE format.

Usage:
    from converter_ps3 import convert_ps3_to_win64
    convert_ps3_to_win64("path/to/NPEB01899--<id>", "output_dir")

PS3 saves keep the 4J save data uncompressed in big-endian inside GAMEDATA.
No STFS container, no XMemCompress wrapper. Region chunks are raw deflate
instead of LZX, so the MCR rewriter is a lot simpler than the 360 path.
"""

import struct
import zlib
from pathlib import Path

from converter import (
    _read_hdr_be,
    _parse_ftable_be,
    _sanitise,
    _s32,
    REGION_SECT_COUNT,
    MCR_EXT,
    VALID_VERSIONS,
)


# =============================================================================
# PARAM.SFO parsing - used to recover the world's display name
# =============================================================================

# SFO value format codes
SFO_FMT_UTF8_RAW  = 0x0004   # UTF-8, no null terminator guaranteed
SFO_FMT_UTF8_STR  = 0x0204   # UTF-8, null-terminated string
SFO_FMT_INT32     = 0x0404   # little-endian uint32


def _parse_param_sfo(path: Path) -> dict:
    """
    Minimal PARAM.SFO reader. Returns a dict of key -> value (str or int).
    Returns an empty dict if the file is missing or malformed.
    """
    if not path.exists():
        return {}

    with open(path, 'rb') as f:
        data = f.read()

    if len(data) < 0x14 or data[:4] != b'\x00PSF':
        return {}

    _ver, key_tbl, data_tbl, entries = struct.unpack_from('<IIII', data, 4)

    result: dict = {}
    for i in range(entries):
        e = 0x14 + i * 16
        if e + 16 > len(data):
            break
        key_off, fmt, dlen, _dmax, doff = struct.unpack_from('<HHIII', data, e)

        ka = key_tbl + key_off
        try:
            ke = data.index(b'\x00', ka)
        except ValueError:
            continue
        key = data[ka:ke].decode('utf-8', errors='replace')

        da = data_tbl + doff
        if fmt in (SFO_FMT_UTF8_RAW, SFO_FMT_UTF8_STR):
            val = data[da:da + dlen].split(b'\x00', 1)[0].decode('utf-8', errors='replace')
        elif fmt == SFO_FMT_INT32:
            val = struct.unpack_from('<I', data, da)[0]
        else:
            val = data[da:da + dlen]

        result[key] = val
    return result


# =============================================================================
# level.dat LevelName patching
# =============================================================================

# On PS3, level.dat's LevelName tag is always the literal placeholder "world"
# - the user-typed world name lives in PARAM.SFO's SUB_TITLE. The Win64 client
# reads LevelName from level.dat; if we leave it as "world" the world list
# ends up showing either "world" or the saveData.ms file stem ("saveData")
# as the world title. Patch the tag in place so the right name shows up.

_LEVELNAME_SIG = b'\x08\x00\x09LevelName'   # TAG_String, name length 9, "LevelName"


def _patch_level_name(nbt: bytes, new_name: str) -> bytes:
    """
    Rewrite the first LevelName TAG_String in *nbt* to *new_name*.
    NBT strings are length-prefixed big-endian UTF-8, so the file can grow
    or shrink; callers must reuse the returned bytes (don't assume the old
    length).
    """
    idx = nbt.find(_LEVELNAME_SIG)
    if idx < 0:
        return nbt

    str_off = idx + len(_LEVELNAME_SIG)
    if str_off + 2 > len(nbt):
        return nbt

    old_len = struct.unpack_from('>H', nbt, str_off)[0]
    end     = str_off + 2 + old_len
    new_val = new_name.encode('utf-8')
    # NBT TAG_String caps at 65535 bytes.
    if len(new_val) > 0xFFFF:
        new_val = new_val[:0xFFFF]
    payload = struct.pack('>H', len(new_val)) + new_val
    return nbt[:str_off] + payload + nbt[end:]


# =============================================================================
# Region file conversion (raw-deflate chunks -> zlib chunks)
# =============================================================================

def _convert_region_ps3(data: bytes, log=None) -> bytes:
    """
    Convert a big-endian PS3 region file to little-endian Win64 format.
    Each chunk is re-encoded from 'BE 4-byte uncomp size + raw deflate'
    into 'zlib' so the PC client can read it.
    """
    SECT = 4096

    buf = bytearray(data)
    # Flip the location table and the timestamp table (1024 entries each, u32).
    for i in range(REGION_SECT_COUNT * 2):
        _s32(buf, i * 4)

    chunk_positions = {}
    for slot in range(REGION_SECT_COUNT):
        off = struct.unpack_from('<I', buf, slot * 4)[0]
        if off == 0:
            continue
        sn    = (off >> 8) & 0xFFFFFF
        count = off & 0xFF
        if sn < 2:
            continue
        fo = sn * SECT
        if fo + 8 > len(buf):
            continue
        chunk_positions[fo] = (slot, sn, count)

    if not chunk_positions:
        return bytes(buf)

    new_buf     = bytearray(len(buf))
    new_buf[:SECT * 2] = buf[:SECT * 2]
    next_sector = 2

    for fo in sorted(chunk_positions.keys()):
        slot, _sn, _count = chunk_positions[fo]

        raw_comp_len   = struct.unpack_from('>I', data, fo)[0]
        raw_decomp_len = struct.unpack_from('>I', data, fo + 4)[0]

        use_rle    = bool(raw_comp_len & 0x80000000)
        comp_len   = raw_comp_len & 0x7FFFFFFF
        decomp_len = raw_decomp_len

        if comp_len == 0 or fo + 8 + comp_len > len(data):
            continue

        chunk_body = data[fo + 8 : fo + 8 + comp_len]

        # PS3 layout inside a chunk: [4B BE uncomp size][raw deflate stream]
        if len(chunk_body) < 5:
            struct.pack_into('<I', new_buf, slot * 4, 0)
            continue

        try:
            # -15 window = raw deflate, no zlib header
            rle_data = zlib.decompress(chunk_body[4:], -15)
        except zlib.error:
            # Corrupt chunk - drop it so the world still loads
            struct.pack_into('<I', new_buf, slot * 4, 0)
            continue

        zlib_data    = zlib.compress(rle_data, 6)
        new_comp_len = len(zlib_data)

        needed   = ((8 + new_comp_len + SECT - 1) // SECT)
        dest_off = next_sector * SECT

        while dest_off + 8 + new_comp_len > len(new_buf):
            new_buf.extend(b'\x00' * SECT)

        struct.pack_into('<I', new_buf, dest_off,
                         new_comp_len | (0x80000000 if use_rle else 0))
        struct.pack_into('<I', new_buf, dest_off + 4, decomp_len)
        new_buf[dest_off + 8 : dest_off + 8 + new_comp_len] = zlib_data

        new_off = (next_sector << 8) | needed
        struct.pack_into('<I', new_buf, slot * 4, new_off)
        next_sector += needed

    return bytes(new_buf[: next_sector * SECT])


# =============================================================================
# Main conversion pipeline
# =============================================================================

def _looks_like_4j_header(data: bytes) -> bool:
    """Cheap sanity check - reject encrypted GAMEDATA files early."""
    if len(data) < 12:
        return False
    ho, ne, _ov, cv = _read_hdr_be(data)
    if ho < 12 or ho >= len(data):
        return False
    # File table must fit exactly after the declared header offset.
    if len(data) - ho != ne * 144:
        return False
    if ne == 0 or ne > 4096:
        return False
    if cv < 0 or cv > 20:
        return False
    return True


def convert_ps3_to_win64(ps3_save_dir: str, game_dir: str,
                         log=None, save_folder: str | None = None) -> str:
    """
    Full PS3 -> Win64 pipeline.  *ps3_save_dir* should be a folder like
    NPEB01899--<id> containing GAMEDATA / PARAM.SFO / THUMB.
    """
    def out(msg):
        if log:
            log(msg)
        else:
            print(msg)

    src = Path(ps3_save_dir)
    if not src.is_dir():
        raise RuntimeError(
            f"'{src}' is not a folder. Pick the PS3 save folder "
            "(the one that contains GAMEDATA, PARAM.SFO, THUMB)."
        )

    gamedata_path = src / 'GAMEDATA'
    if not gamedata_path.exists():
        raise RuntimeError(
            f"GAMEDATA not found in {src.name}.\n"
            "Make sure this is a PS3 Minecraft LCE save folder."
        )

    # Display name comes from PARAM.SFO's SUB_TITLE (the world name the
    # user typed in-game). Fall back to the folder name if the SFO is
    # missing or unreadable.
    sfo = _parse_param_sfo(src / 'PARAM.SFO')
    save_title = sfo.get('SUB_TITLE') or src.name
    out(f"  Save name   : {save_title}")

    out(f"Reading  {gamedata_path.name} ...")
    with open(gamedata_path, 'rb') as f:
        decompressed = f.read()
    out(f"  Size        : {len(decompressed):,} bytes")

    if not _looks_like_4j_header(decompressed):
        raise RuntimeError(
            "GAMEDATA does not look like a valid Minecraft PS3 save.\n"
            "Double-check you picked the right folder - it should contain "
            "GAMEDATA, PARAM.SFO and THUMB."
        )

    ho, ne, ov, cv = _read_hdr_be(decompressed)
    out(f"  Header      : offset=0x{ho:08X}  entries={ne}  ver={ov}->{cv}")
    if cv not in VALID_VERSIONS:
        out(f"  [!] Save version {cv} is outside the expected range (2-9).")
        out("    This save may be from a version that isn't supported by TU19.")

    out("Converting  PS3 (BE) -> Windows 64 (LE) ...")

    entries = _parse_ftable_be(decompressed, ho, ne)

    file_blobs: list[bytes] = []
    for e in entries:
        fn   = e['filename']
        s, l = e['start_offset'], e['length']
        raw_file = decompressed[s : s + l] if s + l <= len(decompressed) else b''
        if fn.lower().endswith(MCR_EXT) and len(raw_file) > 0:
            out(f"  Region file : {fn}")
            raw_file = _convert_region_ps3(raw_file, log=log)
        elif fn.lower() == 'level.dat' and save_title and raw_file:
            patched = _patch_level_name(raw_file, save_title)
            if patched is not raw_file:
                out(f"  level.dat   : LevelName -> {save_title!r}")
            else:
                out(f"  Keeping     : {fn}")
            raw_file = patched
        else:
            out(f"  Keeping     : {fn}")
        file_blobs.append(raw_file)

    HEADER_SIZE = 12
    body        = bytearray()
    new_entries = []
    cursor      = HEADER_SIZE

    for i, e in enumerate(entries):
        blob = file_blobs[i]
        new_entries.append({
            'filename': e['filename'],
            'length':   len(blob),
            'start':    cursor,
            'last_mod': e['last_mod'],
        })
        body.extend(blob)
        cursor += len(blob)

    new_fto = cursor

    ftable = bytearray()
    for ne_entry in new_entries:
        fn_bytes  = ne_entry['filename'].encode('utf-16-le')
        fn_padded = fn_bytes[:128].ljust(128, b'\x00')
        ftable.extend(fn_padded)
        ftable.extend(struct.pack('<I', ne_entry['length']))
        ftable.extend(struct.pack('<I', ne_entry['start']))
        ftable.extend(struct.pack('<q', ne_entry['last_mod']))

    WIN64_SAVE_VERSION = 9
    header  = struct.pack('<I', new_fto)
    header += struct.pack('<I', ne)
    header += struct.pack('<h', ov)
    header += struct.pack('<h', WIN64_SAVE_VERSION)

    raw_le = bytes(header) + bytes(body) + bytes(ftable)
    out(f"  Converted   : {len(raw_le):,} bytes (uncompressed)")

    out("Compressing with zlib ...")
    compressed = zlib.compress(raw_le, level=6)
    win64      = struct.pack('<II', 0, len(raw_le)) + compressed
    out(f"  Output      : {len(win64):,} bytes  ({len(compressed):,} compressed)")

    folder = save_folder or _sanitise(save_title)
    dst    = Path(game_dir) / folder
    out(f"Writing to  {dst}")
    dst.mkdir(parents=True, exist_ok=True)

    (dst / 'saveData.ms').write_bytes(win64)
    out("  saveData.ms  [ok]")

    thumb_path = src / 'THUMB'
    if thumb_path.exists():
        thumb_bytes = thumb_path.read_bytes()
        if thumb_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            (dst / 'thumbnails').mkdir(exist_ok=True)
            (dst / 'thumbnails' / 'thumbData.png').write_bytes(thumb_bytes)
            out("  thumbnails/thumbData.png [ok]")

    out(f"\nInstalled!  ->  {dst}")
    return str(dst)
