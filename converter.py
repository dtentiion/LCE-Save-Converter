"""
LCE Save Converter - Backend
Converts Xbox 360 (.bin STFS CON) save files to Windows64 LCE format.

This module contains all conversion logic with no GUI dependencies.
Can be used standalone:
    from converter import convert_bin_to_win64
    convert_bin_to_win64("save.bin", "output_dir")
"""

import struct
import re
import zlib
import ctypes
from pathlib import Path


# =============================================================================
# STFS container parsing
# =============================================================================

STFS_MAGIC          = b'CON '
STFS_BASE_OFFSET    = 0xA000
STFS_BLOCK_SIZE     = 0x1000
STFS_BLOCKS_PER_GRP = 170


def _stfs_block_offset(block_num: int, table_shift: int) -> int:
    """Byte offset of STFS data block in the file."""
    g = block_num // STFS_BLOCKS_PER_GRP
    hash_before = 2 if g == 0 else 3 + 2 * g + table_shift
    return STFS_BASE_OFFSET + (block_num + hash_before) * STFS_BLOCK_SIZE


def _stfs_get_hash_entry(raw: bytes, block_num: int, table_shift: int) -> int:
    """Read the next-block pointer from the STFS level-0 hash table."""
    group = block_num // STFS_BLOCKS_PER_GRP
    # Check both primary (-2 blocks before data) and backup (-1 block) hash tables.
    first_data = _stfs_block_offset(group * STFS_BLOCKS_PER_GRP, table_shift)
    idx = (block_num % STFS_BLOCKS_PER_GRP) * 0x18

    for gap in (2, 1):
        hash_off = first_data - STFS_BLOCK_SIZE * gap
        entry_off = hash_off + idx
        if entry_off + 0x18 > len(raw):
            continue
        nxt = (raw[entry_off + 0x15] << 16) | (raw[entry_off + 0x16] << 8) | raw[entry_off + 0x17]
        if nxt != 0xFFFFFF:
            return nxt
    return -1


def _stfs_read_file(raw: bytes, start: int, size: int, table_shift: int) -> bytes:
    """Read a file from STFS by following the block chain."""
    max_block = (len(raw) - STFS_BASE_OFFSET) // STFS_BLOCK_SIZE
    out = bytearray()
    blk, rem = start, size
    while rem > 0:
        off = _stfs_block_offset(blk, table_shift)
        chunk = raw[off:off + STFS_BLOCK_SIZE]
        take  = min(rem, STFS_BLOCK_SIZE)
        out.extend(chunk[:take])
        rem -= take
        nxt = _stfs_get_hash_entry(raw, blk, table_shift)
        # Valid chain entry: positive, in range, and not pointing backwards
        if 0 < nxt < max_block:
            blk = nxt
        else:
            blk += 1
    return bytes(out)


class STFSPackage:
    DISP_OFF = 0x0411
    DISP_LEN = 128
    THUMB_OFF = 0x171A

    def __init__(self, raw: bytes):
        if raw[:4] != STFS_MAGIC:
            raise ValueError(f"Not an STFS CON file (magic={raw[:4]!r})")
        self.raw = raw
        self.table_shift = self._detect_table_shift(raw)
        self._name  = None
        self._thumb = None
        self._table = None

    def _detect_table_shift(self, raw: bytes) -> int:
        """Determine table size shift. All tested Xbox 360 CON saves use 1."""
        return 1

    def _read_block(self, b: int) -> bytes:
        o = _stfs_block_offset(b, self.table_shift)
        return self.raw[o:o + STFS_BLOCK_SIZE]

    @property
    def display_name(self) -> str:
        if self._name is None:
            nb = self.raw[self.DISP_OFF: self.DISP_OFF + self.DISP_LEN * 2]
            try:
                self._name = nb.decode('utf-16-be').split('\x00')[0]
            except Exception:
                self._name = "Unknown"
        return self._name

    @property
    def thumbnail(self) -> bytes | None:
        if self._thumb is not None:
            return self._thumb
        PNG = b'\x89PNG\r\n\x1a\n'
        idx = self.raw.find(PNG, self.THUMB_OFF)
        if idx == -1:
            return None
        iend = self.raw.find(b'IEND', idx)
        if iend == -1:
            return None
        self._thumb = self.raw[idx: iend + 12]
        return self._thumb

    @property
    def file_table(self):
        if self._table is None:
            self._table = self._parse_table()
        return self._table

    def _parse_table(self):
        ft_block = (self.raw[0x37E] |
                    (self.raw[0x37F] << 8) |
                    (self.raw[0x380] << 16))
        entries  = []
        blk_data = self._read_block(ft_block)
        for i in range(64):
            e        = blk_data[i*64:(i+1)*64]
            name_len = e[0x28] & 0x3F
            if name_len == 0:
                continue
            if (e[0x28] >> 6) & 0x02:   # directory flag
                continue
            name        = e[:name_len].decode('ascii', errors='replace')
            start_block = e[0x2F] | (e[0x30] << 8) | (e[0x31] << 16)
            file_size   = struct.unpack_from('>I', e, 0x34)[0]
            if name and file_size > 0:
                entries.append((name, start_block, file_size))
        return entries

    def extract_savegame_dat(self) -> bytes | None:
        for cand in ('savegame.dat', 'SAVEGAME.DAT'):
            for (n, sb, sz) in self.file_table:
                if n.lower() == cand:
                    return _stfs_read_file(self.raw, sb, sz, self.table_shift)
        return None


# =============================================================================
# 4J save format helpers
# =============================================================================

FILE_ENTRY_SIZE   = 144
REGION_SECT_COUNT = 1024
MCR_EXT           = '.mcr'
VALID_VERSIONS    = set(range(2, 10))

def _s32(b, o):  b[o:o+4] = b[o:o+4][::-1]

def _read_hdr_be(data):
    ho = struct.unpack_from('>I', data, 0)[0]
    ne = struct.unpack_from('>I', data, 4)[0]
    ov = struct.unpack_from('>h', data, 8)[0]
    cv = struct.unpack_from('>h', data, 10)[0]
    return ho, ne, ov, cv

def _parse_ftable_be(data, ho, ne):
    out = []
    for i in range(ne):
        base = ho + i * FILE_ENTRY_SIZE
        raw  = data[base: base + FILE_ENTRY_SIZE]
        try:
            fn = raw[:128].decode('utf-16-be').split('\x00')[0]
        except Exception:
            fn = ''
        out.append({
            'filename':     fn,
            'length':       struct.unpack_from('>I', raw, 128)[0],
            'start_offset': struct.unpack_from('>I', raw, 132)[0],
            'last_mod':     struct.unpack_from('>q', raw, 136)[0],
            'raw_offset':   base,
        })
    return out

def _sanitise(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    s = re.sub(r'_+', '_', s).strip('_. ')
    return s[:64] or 'MinecraftSave'


# =============================================================================
# LZX decompression (native DLLs)
# =============================================================================

LZX_BLOCK_SIZE  = 0x8000          # 32 768 bytes uncompressed per XMem block
LZX_WINDOW_SIZE = 128 * 1024      # 131 072

# -- CHMLib LZX (for region chunks) --

_CHM_LZX_DLL = None
_CHM_LZX_PATH = Path(__file__).parent / 'chm_lzx.dll'

def _get_chm_lzx():
    global _CHM_LZX_DLL
    if _CHM_LZX_DLL is not None:
        return _CHM_LZX_DLL
    if not _CHM_LZX_PATH.exists():
        raise FileNotFoundError(f"chm_lzx.dll not found at {_CHM_LZX_PATH}")
    dll = ctypes.CDLL(str(_CHM_LZX_PATH))
    dll.chm_lzx_init.restype  = ctypes.c_void_p
    dll.chm_lzx_init.argtypes = [ctypes.c_int]
    dll.chm_lzx_decompress.restype  = ctypes.c_int
    dll.chm_lzx_decompress.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    dll.chm_lzx_teardown.restype  = None
    dll.chm_lzx_teardown.argtypes = [ctypes.c_void_p]
    _CHM_LZX_DLL = dll
    return dll


def _decompress_region_chunk(xbox_data: bytes) -> bytes:
    """
    Decompress a single Xbox 360 region chunk (mini XMemCompress stream)
    using the CHMLib LZX decoder.
    Returns the RLE-encoded intermediate data.
    """
    hi = xbox_data[0]
    if hi == 0xFF:
        output_size = (xbox_data[1] << 8) | xbox_data[2]
        src_sz      = (xbox_data[3] << 8) | xbox_data[4]
        lzx_raw     = xbox_data[5 : 5 + src_sz]
    else:
        src_sz      = (hi << 8) | xbox_data[1]
        output_size = LZX_BLOCK_SIZE
        lzx_raw     = xbox_data[2 : 2 + src_sz]

    dll   = _get_chm_lzx()
    state = dll.chm_lzx_init(17)      # window = 2^17 = 131072
    if not state:
        raise RuntimeError("chm_lzx_init failed")

    try:
        src_buf = (ctypes.c_ubyte * len(lzx_raw)).from_buffer_copy(lzx_raw)
        dst_buf = (ctypes.c_ubyte * output_size)()
        ret = dll.chm_lzx_decompress(state, src_buf, len(lzx_raw),
                                      dst_buf, output_size)
        if ret != 0:
            raise RuntimeError(f"chm_lzx_decompress failed (ret={ret})")
        return bytes(dst_buf[:output_size])
    finally:
        dll.chm_lzx_teardown(state)


# -- GoobyCorp LDI (for save-level decompression) --

_LZX_DLL = None
_LZX_DLL_PATH = Path(__file__).parent / 'LZXDecompression.dll'

def _get_lzx_dll():
    global _LZX_DLL
    if _LZX_DLL is not None:
        return _LZX_DLL
    if not _LZX_DLL_PATH.exists():
        raise FileNotFoundError(
            f"LZXDecompression.dll not found at {_LZX_DLL_PATH}\n"
            "This DLL is required for Xbox 360 save decompression."
        )
    dll = ctypes.CDLL(str(_LZX_DLL_PATH))
    ULONG  = ctypes.c_uint32
    LONG   = ctypes.c_int32
    MHND   = ctypes.c_longlong
    dll.LDICreateDecompression.argtypes  = [ctypes.POINTER(ULONG), ctypes.c_void_p,
                                             ctypes.POINTER(ULONG), ctypes.POINTER(MHND)]
    dll.LDICreateDecompression.restype   = LONG
    dll.LDIDecompress.argtypes           = [MHND, ctypes.c_void_p, LONG,
                                             ctypes.c_void_p, ctypes.POINTER(ULONG)]
    dll.LDIDecompress.restype            = LONG
    dll.LDIDestroyDecompression.argtypes = [MHND]
    dll.LDIDestroyDecompression.restype  = LONG
    _LZX_DLL = dll
    return dll


def _ldi_decompress_chunks(chunk_data: bytes, uncomp_total: int,
                            *, has_header: bool = True,
                            block_max: int = LZX_BLOCK_SIZE) -> bytes:
    """
    Decompress XMemCompress-framed LZX data using the native LDI library.

    If has_header=True:  chunk_data starts with [4B uncomp_total BE][chunks]
    If has_header=False: chunk_data starts directly with chunks.

    Each chunk has a 2-byte header (src_sz, dst=block_max) or a
    5-byte 0xFF header (explicit dst_sz + src_sz).
    """
    dll = _get_lzx_dll()
    ULONG = ctypes.c_uint32
    LONG  = ctypes.c_int32
    MHND  = ctypes.c_longlong

    class _LZXD(ctypes.LittleEndianStructure):
        _pack_  = 2
        _fields_ = [('WindowSize', ctypes.c_int32),
                     ('fCPUtype',   ctypes.c_int32)]

    params = _LZXD()
    params.WindowSize = LZX_WINDOW_SIZE
    params.fCPUtype   = 1

    ctx = MHND()
    bm  = ULONG(block_max)
    sm  = ULONG(0)
    hr  = dll.LDICreateDecompression(ctypes.byref(bm), ctypes.byref(params),
                                      ctypes.byref(sm), ctypes.byref(ctx))
    if hr != 0:
        raise RuntimeError(f"LDICreateDecompression failed (0x{hr:X})")

    try:
        output = bytearray()
        pos    = 4 if has_header else 0
        n      = len(chunk_data)

        while pos < n and len(output) < uncomp_total:
            hi = chunk_data[pos]
            if hi == 0xFF:
                if pos + 5 > n: break
                dst_sz = (chunk_data[pos+1] << 8) | chunk_data[pos+2]
                src_sz = (chunk_data[pos+3] << 8) | chunk_data[pos+4]
                pos += 5
            else:
                if pos + 2 > n: break
                src_sz = (hi << 8) | chunk_data[pos+1]
                dst_sz = block_max
                pos += 2

            if src_sz == 0 or dst_sz == 0: break
            if pos + src_sz > n: break

            remaining = uncomp_total - len(output)
            out_sz    = min(dst_sz, remaining)

            src_buf = (ctypes.c_ubyte * src_sz).from_buffer_copy(
                          chunk_data[pos : pos + src_sz])
            dst_buf = (ctypes.c_ubyte * out_sz)()
            ds      = ULONG(out_sz)

            hr = dll.LDIDecompress(ctx, src_buf, LONG(src_sz),
                                    dst_buf, ctypes.byref(ds))
            if hr != 0:
                raise RuntimeError(
                    f"LDIDecompress failed at offset {pos} "
                    f"(src={src_sz}, dst={out_sz}, hr={hr})")

            output.extend(bytes(dst_buf[: ds.value]))
            pos += src_sz

        return bytes(output)

    finally:
        dll.LDIDestroyDecompression(ctx)


def _lzx_decompress_native(lzx_stream: bytes, uncomp_total: int) -> bytes:
    """Decompress a save-level XMemCompress stream (with 4B header)."""
    return _ldi_decompress_chunks(lzx_stream, uncomp_total,
                                   has_header=True, block_max=LZX_BLOCK_SIZE)


# =============================================================================
# Region file conversion
# =============================================================================

def _convert_region(data: bytes, log=None) -> bytes:
    """
    Convert a big-endian Xbox 360 region file to little-endian Win64 format.
    Decompresses each chunk's LZX data and recompresses with zlib.
    """
    SECT = 4096

    buf = bytearray(data)
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

    try:
        _get_lzx_dll()
    except FileNotFoundError:
        for fo in chunk_positions:
            _s32(buf, fo)
            _s32(buf, fo + 4)
        return bytes(buf)

    new_buf     = bytearray(len(buf))
    new_buf[:SECT * 2] = buf[:SECT * 2]
    next_sector = 2

    for fo in sorted(chunk_positions.keys()):
        slot, sn, count = chunk_positions[fo]

        raw_comp_len  = struct.unpack_from('>I', data, fo)[0]
        raw_decomp_len = struct.unpack_from('>I', data, fo + 4)[0]

        use_rle   = bool(raw_comp_len & 0x80000000)
        comp_len  = raw_comp_len & 0x7FFFFFFF
        decomp_len = raw_decomp_len

        if comp_len == 0 or fo + 8 + comp_len > len(data):
            continue

        xbox_data = data[fo + 8 : fo + 8 + comp_len]

        rle_data = None
        try:
            rle_data = _decompress_region_chunk(xbox_data)
        except Exception:
            pass

        if rle_data is None:
            struct.pack_into('<I', new_buf, slot * 4, 0)
            continue

        zlib_data = zlib.compress(rle_data, 6)
        new_comp_len = len(zlib_data)

        needed = ((8 + new_comp_len + SECT - 1) // SECT)
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
# XMemCompress decompression
# =============================================================================

def _decompress_xmemcompress(dat: bytes) -> bytes:
    """
    Decompress an Xbox 360 XMemCompress/LZX stream from savegame.dat.
    """
    meta_off     = struct.unpack_from('>I', dat, 0)[0]
    lzx_stream   = dat[8:meta_off]
    uncomp_total = struct.unpack_from('>I', lzx_stream, 0)[0]

    return _lzx_decompress_native(lzx_stream, uncomp_total)


# =============================================================================
# Main conversion pipeline
# =============================================================================

def convert_bin_to_win64(bin_path: str, game_dir: str,
                          log=None, save_folder: str | None = None) -> str:
    """
    Full pipeline.  *log* is an optional callable(str) for progress messages.
    Returns the output folder path.
    """
    def out(msg):
        if log:
            log(msg)
        else:
            print(msg)

    out(f"Reading  {Path(bin_path).name} ...")
    with open(bin_path, 'rb') as f:
        raw = f.read()

    pkg  = STFSPackage(raw)
    name = pkg.display_name
    out(f"  Save name   : {name}")

    out("Extracting savegame.dat ...")
    dat = pkg.extract_savegame_dat()
    if dat is None:
        raise RuntimeError(
            "savegame.dat not found in the STFS package.\n"
            "Make sure this is a valid Xbox 360 Minecraft LCE save."
        )
    out(f"  Size        : {len(dat):,} bytes")

    out("Decompressing XMemCompress (LZX) stream ...")
    decompressed = _decompress_xmemcompress(dat)
    out(f"  Decompressed: {len(decompressed):,} bytes")

    ho, ne, ov, cv = _read_hdr_be(decompressed)
    out(f"  Header      : offset=0x{ho:08X}  entries={ne}  ver={ov}->{cv}")
    if cv not in VALID_VERSIONS:
        out(f"  [!] Save version {cv} is outside the expected range (2-9).")
        out(f"    This save may be from an early TU that isn't supported by TU19.")

    out("Converting  Xbox 360 (BE) -> Windows 64 (LE) ...")

    entries = _parse_ftable_be(decompressed, ho, ne)

    file_blobs: list[bytes] = []
    for e in entries:
        fn = e['filename']
        s, l = e['start_offset'], e['length']
        raw_file = decompressed[s : s + l] if s + l <= len(decompressed) else b''
        if fn.lower().endswith(MCR_EXT) and len(raw_file) > 0:
            out(f"  Region file : {fn}")
            raw_file = _convert_region(raw_file)
        else:
            out(f"  Keeping     : {fn}")
        file_blobs.append(raw_file)

    HEADER_SIZE = 12
    body = bytearray()
    new_entries = []
    cursor = HEADER_SIZE

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
        fn_bytes = ne_entry['filename'].encode('utf-16-le')
        fn_padded = fn_bytes[:128].ljust(128, b'\x00')
        ftable.extend(fn_padded)
        ftable.extend(struct.pack('<I', ne_entry['length']))
        ftable.extend(struct.pack('<I', ne_entry['start']))
        ftable.extend(struct.pack('<q', ne_entry['last_mod']))

    WIN64_SAVE_VERSION = 9
    header = struct.pack('<I', new_fto)
    header += struct.pack('<I', ne)
    header += struct.pack('<h', ov)
    header += struct.pack('<h', WIN64_SAVE_VERSION)

    raw_le = bytes(header) + bytes(body) + bytes(ftable)
    out(f"  Converted   : {len(raw_le):,} bytes (uncompressed)")

    out("Compressing with zlib ...")
    compressed = zlib.compress(raw_le, level=6)
    win64 = struct.pack('<II', 0, len(raw_le)) + compressed
    out(f"  Output      : {len(win64):,} bytes  ({len(compressed):,} compressed)")

    folder = save_folder or _sanitise(name)
    dst    = Path(game_dir) / folder
    out(f"Writing to  {dst}")
    dst.mkdir(parents=True, exist_ok=True)

    (dst / 'saveData.ms').write_bytes(win64)
    (dst / 'worldname.txt').write_text(name, encoding='utf-8')
    out("  saveData.ms  [ok]")
    out("  worldname.txt [ok]")

    thumb = pkg.thumbnail
    if thumb:
        (dst / 'thumbnails').mkdir(exist_ok=True)
        (dst / 'thumbnails' / 'thumbData.png').write_bytes(thumb)
        out("  thumbnails/thumbData.png [ok]")

    out(f"\nInstalled!  ->  {dst}")
    return str(dst)
