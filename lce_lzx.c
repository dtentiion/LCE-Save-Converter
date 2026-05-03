/*
 * lce_lzx.c - thin ctypes-friendly wrapper around libmspack's LZX decoder.
 *
 * Exposes a single export, lce_lzxd_decompress, that takes a source byte
 * buffer + a destination byte buffer and runs the same code path Xenia uses
 * to decode Xbox 360 LZX-compressed region chunks. mspack's LZX is more
 * lenient with malformed input than Microsoft's XMemDecompress (returns an
 * error code instead of access-violating), which is why we layer it after
 * the existing CHMLib / LDI / xcompress64 tiers.
 *
 * Build (from this folder, after running vcvars64.bat):
 *   cl /LD /MD /Ox /DLZX_BUILD_DLL ^
 *      /I libmspack/libmspack/mspack ^
 *      lce_lzx.c ^
 *      libmspack/libmspack/mspack/lzxd.c ^
 *      libmspack/libmspack/mspack/system.c ^
 *      /Fe:lce_lzx.dll /link /DEF:lce_lzx.def
 */

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include <mspack.h>
#include <lzx.h>

/* In-memory mspack_file: read/write/seek over a fixed buffer. */
struct mem_file {
    unsigned char *buf;
    size_t len;
    size_t pos;
    int    write_mode;
};

static struct mspack_file *mem_open(struct mspack_system *self,
                                    const char *filename, int mode) {
    /* Not used - we hand pre-built file structs to lzxd_init. */
    (void)self; (void)filename; (void)mode;
    return NULL;
}

static void mem_close(struct mspack_file *file) {
    (void)file;
}

static int mem_read(struct mspack_file *file, void *buffer, int bytes) {
    struct mem_file *f = (struct mem_file *)file;
    size_t avail = f->len - f->pos;
    size_t take  = (size_t)bytes < avail ? (size_t)bytes : avail;
    if (take == 0) return 0;
    memcpy(buffer, f->buf + f->pos, take);
    f->pos += take;
    return (int)take;
}

static int mem_write(struct mspack_file *file, void *buffer, int bytes) {
    struct mem_file *f = (struct mem_file *)file;
    size_t avail = f->len - f->pos;
    size_t take  = (size_t)bytes < avail ? (size_t)bytes : avail;
    if (take == 0) return 0;
    memcpy(f->buf + f->pos, buffer, take);
    f->pos += take;
    return (int)take;
}

static int mem_seek(struct mspack_file *file, off_t offset, int mode) {
    struct mem_file *f = (struct mem_file *)file;
    size_t pos;
    switch (mode) {
        case MSPACK_SYS_SEEK_START:   pos = (size_t)offset; break;
        case MSPACK_SYS_SEEK_CUR:     pos = f->pos + (size_t)offset; break;
        case MSPACK_SYS_SEEK_END:     pos = f->len + (size_t)offset; break;
        default: return -1;
    }
    if (pos > f->len) return -1;
    f->pos = pos;
    return 0;
}

static off_t mem_tell(struct mspack_file *file) {
    struct mem_file *f = (struct mem_file *)file;
    return (off_t)f->pos;
}

static void mem_msg(struct mspack_file *file, const char *format, ...) {
    (void)file; (void)format;
}

static void *mem_alloc(struct mspack_system *self, size_t bytes) {
    (void)self;
    return malloc(bytes);
}

static void mem_free(void *buffer) {
    free(buffer);
}

static void mem_copy(void *src, void *dest, size_t bytes) {
    memcpy(dest, src, bytes);
}

static struct mspack_system mem_sys = {
    mem_open, mem_close, mem_read, mem_write,
    mem_seek, mem_tell, mem_msg, mem_alloc, mem_free, mem_copy, NULL
};

/*
 * Decompress one LZX-compressed buffer into a destination buffer.
 *
 * src/src_len:    raw LZX bitstream (caller strips any framing)
 * dst/dst_len:    output buffer; dst_len must be >= the expected
 *                 uncompressed size
 * window_bits:    LZX window size as a power of two (15..21)
 * out_actual:     receives bytes actually written
 *
 * Returns 0 on success, non-zero MSPACK_ERR_* code on failure.
 */
__declspec(dllexport)
int lce_lzxd_decompress(const unsigned char *src, size_t src_len,
                        unsigned char *dst, size_t dst_len,
                        int window_bits, size_t *out_actual) {
    struct mem_file in  = { (unsigned char *)src, src_len, 0, 0 };
    struct mem_file out = { dst,                  dst_len, 0, 1 };

    struct lzxd_stream *lzxd = lzxd_init(
        &mem_sys,
        (struct mspack_file *)&in,
        (struct mspack_file *)&out,
        window_bits,
        0,                                 /* reset_interval */
        4096,                              /* input buffer */
        (off_t)dst_len,                    /* output length */
        0                                  /* is_delta */
    );

    if (!lzxd) {
        if (out_actual) *out_actual = 0;
        return MSPACK_ERR_NOMEMORY;
    }

    int rc = lzxd_decompress(lzxd, (off_t)dst_len);
    if (out_actual) *out_actual = out.pos;
    lzxd_free(lzxd);
    return rc;
}
