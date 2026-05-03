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
# Region filename + level.dat spawn helpers
# =============================================================================
#
# Some saves contain individual region chunks whose LZX data is corrupt
# at the bit level - no LZX decoder can read them. The converter drops
# these chunks (their slot in the location table is zeroed). On Win64
# the client crashes if a missing chunk falls inside the player's load
# radius around spawn. To make the world loadable, we shift spawn to a
# chunk far enough from any drop that the load radius doesn't touch
# them. The corrupt bytes stay in the region file untouched - if the
# player ever walks back to that area they'll hit the same crash, but
# under normal play the world loads and the rest of the terrain, items,
# chests, etc. are preserved. This logic only fires when chunks are
# actually dropped; saves with no drops convert exactly as before.

_REGION_FNAME_RE = {
    'end':       re.compile(r'^dim1[/\\]r\.(-?\d+)\.(-?\d+)\.mcr$'),
    'nether':    re.compile(r'^dim-1r\.(-?\d+)\.(-?\d+)\.mcr$'),
    'overworld': re.compile(r'^r\.(-?\d+)\.(-?\d+)\.mcr$'),
}


def _parse_region_filename(name: str) -> tuple[str, int, int] | None:
    """
    Returns (dimension, region_x, region_z) where dimension is one of
    'overworld' / 'nether' / 'end'. None if the name isn't a region file.
    """
    n = name.lower()
    for dim, regex in _REGION_FNAME_RE.items():
        m = regex.match(n)
        if m:
            return (dim, int(m.group(1)), int(m.group(2)))
    return None


# NBT TAG_Int signature for SpawnX / SpawnY / SpawnZ:
#   0x03 (TAG_Int) | 0x0006 (name length BE) | "SpawnX|Y|Z" | 4 bytes BE int
def _spawn_sig(axis: str) -> bytes:
    return b'\x03\x00\x06Spawn' + axis.encode('ascii')


def _read_spawn(level_dat: bytes) -> tuple[int, int, int] | None:
    """Parse SpawnX/Y/Z from a level.dat NBT blob. None if any tag missing."""
    coords = []
    for axis in ('X', 'Y', 'Z'):
        sig = _spawn_sig(axis)
        idx = level_dat.find(sig)
        if idx < 0:
            return None
        val_off = idx + len(sig)
        if val_off + 4 > len(level_dat):
            return None
        coords.append(struct.unpack_from('>i', level_dat, val_off)[0])
    return (coords[0], coords[1], coords[2])


def _patch_spawn(level_dat: bytes, x: int, y: int, z: int) -> bytes:
    """Overwrite SpawnX/Y/Z in level.dat. NBT length is unchanged (TAG_Int=4B)."""
    out = bytearray(level_dat)
    for axis, val in (('X', x), ('Y', y), ('Z', z)):
        sig = _spawn_sig(axis)
        idx = out.find(sig)
        if idx >= 0:
            struct.pack_into('>i', out, idx + len(sig), val)
    return bytes(out)


# Player NBT signature: TAG_List "Pos" of 3 doubles.
#   0x09 (TAG_List) | 0x0003 (name length BE) | "Pos" | 0x06 (element type=double)
#   | 0x00000003 (list length BE)
_PLAYER_POS_SIG = b'\x09\x00\x03Pos\x06\x00\x00\x00\x03'


def _read_player_pos(player_dat: bytes) -> tuple[float, float, float] | None:
    idx = player_dat.find(_PLAYER_POS_SIG)
    if idx < 0:
        return None
    p = idx + len(_PLAYER_POS_SIG)
    if p + 24 > len(player_dat):
        return None
    return struct.unpack_from('>ddd', player_dat, p)


def _patch_player_pos(player_dat: bytes, x: float, y: float, z: float) -> bytes:
    """Overwrite the Pos TAG_List in a player.dat. Length unchanged (3 BE doubles)."""
    out = bytearray(player_dat)
    idx = out.find(_PLAYER_POS_SIG)
    if idx >= 0:
        struct.pack_into('>ddd', out, idx + len(_PLAYER_POS_SIG), x, y, z)
    return bytes(out)


# RLE encoding format used inside region chunks.
#   0..254     -> literal byte
#   255, 0|1|2 -> 1..3 copies of 0xFF
#   255, n>=3  -> (n+1) copies of next byte b (so 255, n, b)
# Symmetric with DecompressRLE in MinecraftConsoles/Minecraft.World/compression.cpp.
def _compress_rle(data: bytes) -> bytes:
    out = bytearray()
    n   = len(data)
    i   = 0
    while i < n:
        b   = data[i]
        run = 1
        while i + run < n and data[i + run] == b and run < 256:
            run += 1
        if b == 0xFF:
            if run <= 3:
                out.append(0xFF)
                out.append(run - 1)         # 0/1/2 = 1/2/3 FFs
            else:
                out.append(0xFF)
                out.append(run - 1)         # 3..255 = 4..256 copies
                out.append(0xFF)
        else:
            if run < 4:
                out.extend([b] * run)
            else:
                out.append(0xFF)
                out.append(run - 1)
                out.append(b)
        i += run
    return bytes(out)


def _build_empty_chunk_nbt(chunk_x: int, chunk_z: int) -> bytes:
    """
    Construct a minimal valid LCE region chunk NBT - all-air blocks,
    full sky light, terrain populated, no entities. Used as a placeholder
    when a chunk's source bytes are corrupt and no LZX library can
    decode them; injecting this lets the world load instead of crashing
    on the missing slot.
    """
    out = bytearray()
    out += b'\x0a\x00\x00'                      # outer compound, no name
    out += b'\x0a\x00\x05Level'                 # Level compound
    # Blocks: 16 * 16 * 128 = 32768 bytes of air (id 0)
    out += b'\x07\x00\x06Blocks' + struct.pack('>i', 32768) + b'\x00' * 32768
    # Data: 4-bit packed = 16384 bytes
    out += b'\x07\x00\x04Data'   + struct.pack('>i', 16384) + b'\x00' * 16384
    # SkyLight: full sun
    out += b'\x07\x00\x08SkyLight'   + struct.pack('>i', 16384) + b'\xff' * 16384
    # BlockLight: dark
    out += b'\x07\x00\x0aBlockLight' + struct.pack('>i', 16384) + b'\x00' * 16384
    # HeightMap: 16x16 = 256
    out += b'\x07\x00\x09HeightMap'  + struct.pack('>i', 256)   + b'\x00' * 256
    # xPos / zPos chunk coords
    out += b'\x03\x00\x04xPos' + struct.pack('>i', chunk_x)
    out += b'\x03\x00\x04zPos' + struct.pack('>i', chunk_z)
    out += b'\x04\x00\x0aLastUpdate' + struct.pack('>q', 0)
    out += b'\x01\x00\x10TerrainPopulated\x01'
    out += b'\x09\x00\x08Entities\x0a\x00\x00\x00\x00'      # empty list of compounds
    out += b'\x09\x00\x0cTileEntities\x0a\x00\x00\x00\x00'  # empty list of compounds
    out += b'\x00'                              # end Level
    out += b'\x00'                              # end outer
    return bytes(out)


def _find_safe_spawn(dropped_chunks: set[tuple[int, int]],
                     orig: tuple[int, int, int],
                     safe_chunks: int = 12) -> tuple[int, int, int] | None:
    """
    Spiral outward from the original spawn looking for a chunk that's at
    least *safe_chunks* (Chebyshev distance) away from any dropped chunk.
    Returns the new (x, y, z) in block coords or None if the original
    spawn is already safe / no spiral position found.
    """
    if not dropped_chunks:
        return None
    sx, sy, sz = orig
    sc_x, sc_z = sx >> 4, sz >> 4

    def too_close(cx: int, cz: int) -> bool:
        for dx, dz in dropped_chunks:
            if max(abs(cx - dx), abs(cz - dz)) < safe_chunks:
                return True
        return False

    if not too_close(sc_x, sc_z):
        return None

    for radius in range(1, 200):
        for dz in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if max(abs(dx), abs(dz)) != radius:
                    continue
                cx = sc_x + dx
                cz = sc_z + dz
                if not too_close(cx, cz):
                    return (cx * 16 + 8, sy, cz * 16 + 8)
    return None


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


# -- Microsoft XCompress (XMemDecompress) - the real API used by the Xbox 360
#    firmware and the LCE PC build. Optional fallback tier: only active if
#    xcompress64.dll is present beside the other DLLs. When it is, it gets a
#    turn in the chunk fallback chain and recovers chunks the open-source
#    decoders miss. Note that Microsoft's LZX is strict about malformed input
#    and can hit access violations on truly corrupt chunks; we wrap the call
#    in OSError handling so a bad chunk just falls through cleanly. --

_XCOMPRESS_DLL = None
_XCOMPRESS_TRIED = False
_XCOMPRESS_PATH = Path(__file__).parent / 'xcompress64.dll'

_XMEM_PAD_BYTES = 0x10000   # zero-padding appended to every input so XMem's
                            # lookahead reads don't run off the end of buffer


class _XMEMCODEC_PARAMETERS_LZX(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("Flags",                    ctypes.c_uint32),
        ("WindowSize",               ctypes.c_uint32),
        ("CompressionPartitionSize", ctypes.c_uint32),
        # The Win64 build of xcompress takes an extended 6-field params
        # struct - extra three uint32s are 0-initialized and unused for
        # decompression but the DLL reads them anyway.
        ("_unk0",                    ctypes.c_uint32),
        ("_unk1",                    ctypes.c_uint32),
        ("_unk2",                    ctypes.c_uint32),
    ]


def _get_xcompress_dll():
    """Lazy-load xcompress64.dll. Returns None if it isn't shipped beside us."""
    global _XCOMPRESS_DLL, _XCOMPRESS_TRIED
    if _XCOMPRESS_TRIED:
        return _XCOMPRESS_DLL
    _XCOMPRESS_TRIED = True
    if not _XCOMPRESS_PATH.exists():
        return None
    try:
        dll = ctypes.WinDLL(str(_XCOMPRESS_PATH))
    except OSError:
        return None
    HRESULT = ctypes.c_int32
    HANDLE  = ctypes.c_void_p

    dll.XMemCreateDecompressionContext.argtypes = [
        ctypes.c_uint32,                            # XMEMCODEC_TYPE
        ctypes.POINTER(_XMEMCODEC_PARAMETERS_LZX),
        ctypes.c_int32,                             # Flags
        ctypes.POINTER(HANDLE),
    ]
    dll.XMemCreateDecompressionContext.restype  = HRESULT

    dll.XMemDecompress.argtypes = [
        HANDLE,
        ctypes.c_char_p,                            # pDestination
        ctypes.POINTER(ctypes.c_uint64),            # pDestSize
        ctypes.c_char_p,                            # pSource
        ctypes.c_uint64,                            # SrcSize
    ]
    dll.XMemDecompress.restype  = ctypes.c_uint32

    dll.XMemDestroyDecompressionContext.argtypes = [HANDLE]
    dll.XMemDestroyDecompressionContext.restype  = None

    _XCOMPRESS_DLL = dll
    return dll


def _try_xcompress_chunk(xbox_data: bytes, output_size: int) -> bytes | None:
    """
    Try Microsoft's XMemDecompress on a region chunk. The full chunk
    bytes go in (including the 2- or 5-byte header) - XMem parses the
    framing itself. Returns the decoded bytes or None on failure.

    Pads the input with zeros so XMem's speculative lookahead reads
    don't run past the end of the heap-allocated buffer (which would
    AV). For chunks with truly malformed LZX data, the decoder may
    still throw an access violation - we catch it as OSError and let
    the caller move on.
    """
    dll = _get_xcompress_dll()
    if dll is None:
        return None

    XMEMCODEC_LZX = 1
    params = _XMEMCODEC_PARAMETERS_LZX(0, 128 * 1024, 128 * 1024, 0, 0, 0)
    ctx = ctypes.c_void_p()
    hr = dll.XMemCreateDecompressionContext(
        XMEMCODEC_LZX, ctypes.byref(params), 0, ctypes.byref(ctx),
    )
    if hr != 0 or not ctx.value:
        return None

    try:
        # Heap padding for speculative reads + a generous output buffer.
        padded = xbox_data + b'\x00' * _XMEM_PAD_BYTES
        cap = max(output_size * 2, LZX_BLOCK_SIZE * 4)
        out_buf = ctypes.create_string_buffer(cap)
        out_sz  = ctypes.c_uint64(cap)
        try:
            hr = dll.XMemDecompress(
                ctx, out_buf, ctypes.byref(out_sz),
                padded, ctypes.c_uint64(len(padded)),
            )
        except OSError:
            return None
        if hr != 0 or out_sz.value == 0:
            return None
        return bytes(out_buf[: out_sz.value])
    finally:
        try:
            dll.XMemDestroyDecompressionContext(ctx)
        except OSError:
            pass


def _try_chm_lzx(lzx_raw: bytes, output_size: int, window: int) -> bytes | None:
    """One CHMLib LZX attempt. Returns the decoded bytes or None on failure."""
    dll   = _get_chm_lzx()
    state = dll.chm_lzx_init(window)
    if not state:
        return None
    try:
        src_buf = (ctypes.c_ubyte * len(lzx_raw)).from_buffer_copy(lzx_raw)
        dst_buf = (ctypes.c_ubyte * output_size)()
        ret = dll.chm_lzx_decompress(state, src_buf, len(lzx_raw),
                                      dst_buf, output_size)
        if ret != 0:
            return None
        return bytes(dst_buf[:output_size])
    finally:
        dll.chm_lzx_teardown(state)


# -- libmspack LZX decoder (lce_lzx.dll) - Stuart Caie's mspack ported with a
#    small wrapper that exposes a single decompress entry point. mspack is
#    what Xenia uses; it tolerates malformed input by returning error codes
#    instead of access-violating, so it usually catches anything that strict
#    decoders reject. --

_MSPACK_DLL = None
_MSPACK_TRIED = False
_MSPACK_PATH = Path(__file__).parent / 'lce_lzx.dll'


def _get_mspack_dll():
    global _MSPACK_DLL, _MSPACK_TRIED
    if _MSPACK_TRIED:
        return _MSPACK_DLL
    _MSPACK_TRIED = True
    if not _MSPACK_PATH.exists():
        return None
    try:
        dll = ctypes.WinDLL(str(_MSPACK_PATH))
    except OSError:
        return None
    dll.lce_lzxd_decompress.argtypes = [
        ctypes.c_char_p,                      # src
        ctypes.c_size_t,                      # src_len
        ctypes.c_char_p,                      # dst
        ctypes.c_size_t,                      # dst_len
        ctypes.c_int,                         # window_bits
        ctypes.POINTER(ctypes.c_size_t),      # out_actual
    ]
    dll.lce_lzxd_decompress.restype = ctypes.c_int
    _MSPACK_DLL = dll
    return dll


def _try_mspack_chunk(lzx_raw: bytes, output_size: int) -> bytes | None:
    """
    Try libmspack on a stripped LZX bitstream. Sweeps window_bits 15..21
    on failure since older saves don't always declare a fixed window.
    Returns the decoded bytes or None if every window rejects the chunk.
    """
    dll = _get_mspack_dll()
    if dll is None:
        return None
    out_buf = ctypes.create_string_buffer(output_size + 16)
    actual  = ctypes.c_size_t(0)
    for wb in (17, 16, 15, 18, 19, 20, 21):
        actual.value = 0
        rc = dll.lce_lzxd_decompress(lzx_raw, len(lzx_raw),
                                     out_buf, output_size, wb,
                                     ctypes.byref(actual))
        if rc == 0 and actual.value == output_size:
            return bytes(out_buf[: actual.value])
    return None


def _try_ldi_chunk(xbox_data: bytes, output_size: int) -> bytes | None:
    """
    Try the GoobyCorp LDI library (closer to XMemDecompress) on a chunk.
    The LDI iterator wants the full chunk including the 2- or 5-byte
    framing header, with no leading uncomp_total prefix.
    """
    try:
        out = _ldi_decompress_chunks(
            xbox_data, output_size, has_header=False, block_max=LZX_BLOCK_SIZE,
        )
    except Exception:
        return None
    if not out:
        return None
    return out[:output_size]


def _decompress_region_chunk(xbox_data: bytes) -> bytes:
    """
    Decompress a single Xbox 360 region chunk (mini XMemCompress stream).
    Returns the RLE-encoded intermediate data.

    Tiered fallback for chunks whose default decode path fails:
      1. CHMLib at window=17 (handles ~99% of TU14+ chunks, fast path)
      2. CHMLib at windows 15..21 (catches non-default windows)
      3. GoobyCorp LDI (a different LZX implementation, close to XMem)
      4. libmspack (lce_lzx.dll) - same decoder Xenia uses; lenient with
         malformed bitstreams, sweeps window sizes
      5. Microsoft xcompress64.dll (real XMemDecompress, optional - only
         attempted if the DLL ships beside us; this is the same API the
         Xbox 360 firmware itself uses, but it's strict about malformed
         input and can hit access violations - we wrap it accordingly)
    Raises RuntimeError if every available path fails - the caller will
    drop the chunk and log it.
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

    # Tier 1: default window. The vast majority of saves stop here.
    out = _try_chm_lzx(lzx_raw, output_size, 17)
    if out is not None:
        return out

    # Tier 2: sweep alternative window sizes. Older saves sometimes use
    # a different window than the modern default.
    for w in (16, 15, 18, 19, 20, 21):
        out = _try_chm_lzx(lzx_raw, output_size, w)
        if out is not None:
            return out

    # Tier 3: try the LDI library. It's the wrapper closest to the
    # XMemDecompress API the real Xbox 360 firmware uses.
    out = _try_ldi_chunk(xbox_data, output_size)
    if out is not None:
        return out

    # Tier 4: libmspack via lce_lzx.dll. Same decoder Xenia uses;
    # tolerates malformed input by returning errors instead of crashing.
    out = _try_mspack_chunk(lzx_raw, output_size)
    if out is not None:
        return out

    # Tier 5: hand off to xcompress64.dll if it's present. This is the
    # actual Microsoft API; it picks up chunks the open-source decoders
    # can't follow but is strict about malformed input.
    out = _try_xcompress_chunk(xbox_data, output_size)
    if out is not None:
        return out

    raise RuntimeError("LZX decode failed (every available decoder rejected the chunk)")


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

def _convert_region(data: bytes, log=None, dropped_slots: list | None = None,
                    region_coords: tuple[int, int] | None = None) -> bytes:
    """
    Convert a big-endian Xbox 360 region file to little-endian Win64 format.
    Decompresses each chunk's LZX data and recompresses with zlib.

    If *dropped_slots* is given, every chunk that all decoder tiers reject
    has its slot index appended. Caller can use this to know which chunks
    to compensate for downstream (e.g. moving spawn away).

    If *region_coords* is given (rx, rz), failed chunks get a synthetic
    empty NBT chunk written at their slot instead of being zeroed. The
    LCE engine pre-generates every chunk in the world border at world
    creation time and crashes on world entry if any expected slot is
    empty - the synthetic chunk has the right shape (all-air, terrain
    populated) and lets the world load. The chunk's original blocks are
    not recovered.
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
        except Exception as exc:
            if log:
                log(f"chunk slot {slot} dropped (LZX decode failed: {exc})")

        if rle_data is None:
            if dropped_slots is not None:
                dropped_slots.append(slot)
            if region_coords is not None:
                # Inject a synthetic empty chunk so the slot isn't blank.
                rx, rz = region_coords
                cx = rx * 32 + (slot % 32)
                cz = rz * 32 + (slot // 32)
                synth_nbt   = _build_empty_chunk_nbt(cx, cz)
                synth_rle   = _compress_rle(synth_nbt)
                synth_zlib  = zlib.compress(synth_rle, 6)
                new_comp_len = len(synth_zlib)
                needed   = ((8 + new_comp_len + SECT - 1) // SECT)
                dest_off = next_sector * SECT
                while dest_off + 8 + new_comp_len > len(new_buf):
                    new_buf.extend(b'\x00' * SECT)
                struct.pack_into('<I', new_buf, dest_off,
                                 new_comp_len | 0x80000000)        # RLE flag set
                struct.pack_into('<I', new_buf, dest_off + 4, len(synth_nbt))
                new_buf[dest_off + 8 : dest_off + 8 + new_comp_len] = synth_zlib
                struct.pack_into('<I', new_buf, slot * 4,
                                 (next_sector << 8) | needed)
                next_sector += needed
                continue
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
    dropped_overworld: set[tuple[int, int]] = set()
    for e in entries:
        fn = e['filename']
        s, l = e['start_offset'], e['length']
        raw_file = decompressed[s : s + l] if s + l <= len(decompressed) else b''
        if fn.lower().endswith(MCR_EXT) and len(raw_file) > 0:
            out(f"  Region file : {fn}")
            slots: list[int] = []
            dim_info = _parse_region_filename(fn)
            # Pass region coords so dropped chunks get a synthetic empty
            # placeholder written at the slot instead of being blank.
            region_xy = (dim_info[1], dim_info[2]) if dim_info else None
            raw_file = _convert_region(
                raw_file,
                log=lambda m, n=fn: out(f"    [!] {n}: {m}"),
                dropped_slots=slots,
                region_coords=region_xy,
            )
            # Only the overworld matters for spawn-load safety.
            if dim_info and dim_info[0] == 'overworld':
                _, rx, rz = dim_info
                for slot in slots:
                    cx = rx * 32 + (slot % 32)
                    cz = rz * 32 + (slot // 32)
                    dropped_overworld.add((cx, cz))
        else:
            out(f"  Keeping     : {fn}")
        file_blobs.append(raw_file)

    # Spawn-rescue: only kicks in when overworld chunks were dropped.
    # Saves with no drops come out byte-identical to the previous behaviour.
    #
    # Two things need patching when a chunk drop sits near where the
    # game wants to load on world entry:
    #   1. level.dat's SpawnX/Y/Z, which is where the world list places
    #      the player on first load
    #   2. every player.dat's Pos tag, which is where each existing
    #      player wakes up when their profile is selected. Hunger Games
    #      maps in particular have dozens of player files clustered in
    #      a small build area; one of them landing in the bad chunk's
    #      load radius is enough to crash on world entry even if the
    #      world spawn itself is fine.
    if dropped_overworld:
        # Decide where "safe" is. Use the world spawn if the bad chunks
        # are far from it; otherwise pick a spiral-out spot.
        new_spawn: tuple[int, int, int] | None = None
        spawn: tuple[int, int, int] | None = None
        for i, e in enumerate(entries):
            if e['filename'].lower() != 'level.dat':
                continue
            spawn = _read_spawn(file_blobs[i])
            if spawn is None:
                break
            new_spawn = _find_safe_spawn(dropped_overworld, spawn)
            if new_spawn is not None:
                file_blobs[i] = _patch_spawn(file_blobs[i], *new_spawn)
            break

        # The point we tell stranded players to wake up at. Prefer the
        # patched spawn; if no patch was needed, we still relocate
        # players who happen to be inside a bad chunk's load radius.
        safe = new_spawn if new_spawn is not None else spawn
        if safe is None:
            out(f"  [!] {len(dropped_overworld)} chunk(s) dropped but no level.dat "
                "spawn to anchor against - skipping rescue.")
        else:
            sx_blk, sy_blk, sz_blk = safe
            sx_chunk, sz_chunk = sx_blk >> 4, sz_blk >> 4

            def _too_close(cx: int, cz: int) -> bool:
                for dx, dz in dropped_overworld:
                    if max(abs(cx - dx), abs(cz - dz)) < 12:
                        return True
                return False

            moved_players = 0
            for i, e in enumerate(entries):
                if not e['filename'].startswith('players/') or not e['filename'].endswith('.dat'):
                    continue
                blob = file_blobs[i]
                pos = _read_player_pos(blob)
                if pos is None:
                    continue
                px, py, pz = pos
                cx, cz = int(px // 16), int(pz // 16)
                if _too_close(cx, cz):
                    file_blobs[i] = _patch_player_pos(
                        blob, float(sx_blk) + 0.5, float(sy_blk), float(sz_blk) + 0.5,
                    )
                    moved_players += 1

            if new_spawn is not None and spawn is not None:
                out(f"  [!] {len(dropped_overworld)} unrecoverable chunk(s) near spawn.")
                out(f"      Spawn moved {spawn} -> {new_spawn} so the world loads on Win64.")
            elif new_spawn is None:
                out(f"  [i] {len(dropped_overworld)} chunk(s) dropped; spawn was already safe.")
            if moved_players:
                out(f"      Relocated {moved_players} player(s) whose Pos was inside a bad "
                    f"chunk's load radius to {safe}.")
                out(f"      Bad chunk slots got a synthetic empty placeholder written - "
                    "world will load, the affected 16x16 area renders as void/air.")

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

    # Preserve the source cv so the game's TU migration code knows what
    # data shape it's looking at. Forcing it to 9 (TU19) on an older
    # save (cv<9) made the client load TU<19-format data as if it were
    # TU19, which crashes at world-load time. Anything above 9 is
    # clamped because the TU19 dev build can't read TU20+ payloads.
    out_cv = cv if cv <= 9 else 9
    if cv > 9:
        out(f"  [!] Source cv={cv} clamped to 9; TU{cv} data may not load correctly.")
    header = struct.pack('<I', new_fto)
    header += struct.pack('<I', ne)
    header += struct.pack('<h', ov)
    header += struct.pack('<h', out_cv)

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
    out("  saveData.ms  [ok]")

    thumb = pkg.thumbnail
    if thumb:
        (dst / 'thumbnails').mkdir(exist_ok=True)
        (dst / 'thumbnails' / 'thumbData.png').write_bytes(thumb)
        out("  thumbnails/thumbData.png [ok]")

    out(f"\nInstalled!  ->  {dst}")
    return str(dst)
