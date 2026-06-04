"""Parse an image's on-disk structures to label byte differences during verify.

Given a raw .img with the typical Raspberry Pi layout (MBR + FAT32 boot partition
+ ext4 root), `ImageMap` locates the byte regions an OS rewrites simply by
mounting a freshly-flashed card (the FAT dirty flag and FSInfo free-space cache,
the ext4 superblock mount fields, and the ext4 journal) so verification can tell
those harmless differences apart from real ones, and name where each mismatch
falls. Everything is parsed from the image file itself: no mounting, no root, and
no assumed layout (offsets come from the partition table and the filesystems'
own superblocks).
"""

from dataclasses import dataclass, field


def _u16(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 2], 'little')


def _u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], 'little')


def human_size(n: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{int(n)} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


@dataclass
class Region:
    """A named byte range [start, end) within the image."""
    start: int
    end: int
    label: str
    benign: bool


@dataclass
class DiffRegion:
    """A slice of a difference, annotated with what structure it falls in."""
    start: int
    end: int
    label: str
    benign: bool

    @property
    def size(self) -> int:
        return self.end - self.start


_FAT_TYPES = {0x01, 0x04, 0x06, 0x0b, 0x0c, 0x0e}

# ext4 superblock fields the kernel rewrites on mount/unmount: (offset, length).
_EXT4_MOUNT_FIELDS = [
    (0x0c, 4),   # s_free_blocks_count_lo
    (0x10, 4),   # s_free_inodes_count
    (0x2c, 4),   # s_mtime
    (0x30, 4),   # s_wtime
    (0x34, 2),   # s_mnt_count
    (0x3a, 2),   # s_state
    (0x40, 4),   # s_lastcheck
    (0xb4, 8),   # s_kbytes_written
    (0x150, 4),  # s_free_blocks_count_hi
    (0x3fc, 4),  # s_checksum
]


class ImageMap:
    """A parsed view of an image's partitions and mount-volatile byte regions."""

    def __init__(self, path: str):
        self.path = path
        self.regions: list[Region] = []        # precise benign ranges, sorted by start
        self.partitions: list[tuple] = []       # (start, end, kind)  kind: fat|ext4|other
        try:
            with open(path, 'rb') as f:
                self._parse(f)
        except Exception:
            pass
        self.regions.sort(key=lambda r: r.start)

    # ── parsing ──────────────────────────────────────────────────────────────

    def _parse(self, f) -> None:
        mbr = f.read(512)
        if mbr[510:512] != b'\x55\xaa':
            return
        for i in range(4):
            e = mbr[446 + i * 16: 462 + i * 16]
            ptype = e[4]
            start_lba = _u32(e, 8)
            nsect = _u32(e, 12)
            if start_lba == 0 or nsect == 0:
                continue
            off = start_lba * 512
            end = off + nsect * 512
            if ptype in _FAT_TYPES:
                self.partitions.append((off, end, 'fat'))
                self._map_fat(f, off)
            else:
                self.partitions.append((off, end, self._map_ext4(f, off)))

    def _map_fat(self, f, off: int) -> None:
        f.seek(off)
        boot = f.read(512)
        if boot[0x52:0x57] == b'FAT32':
            self._add(off + 0x41, off + 0x42, 'FAT dirty flag')
            bps = _u16(boot, 0x0b) or 512
            fsinfo_sec = _u16(boot, 0x30)
            if fsinfo_sec not in (0, 0xffff):
                fsi_off = off + fsinfo_sec * bps
                f.seek(fsi_off)
                fsi = f.read(512)
                if fsi[0:4] == b'RRaA' and fsi[0x1e4:0x1e8] == b'rrAa':
                    self._add(fsi_off + 0x1e8, fsi_off + 0x1f0, 'FAT free-space cache')
        else:
            self._add(off + 0x25, off + 0x26, 'FAT dirty flag')   # FAT12/16

    def _map_ext4(self, f, off: int) -> str:
        f.seek(off + 1024)
        sb = f.read(1024)
        if _u16(sb, 0x38) != 0xEF53:
            return 'other'
        for fo, fl in _EXT4_MOUNT_FIELDS:
            self._add(off + 1024 + fo, off + 1024 + fo + fl, 'ext4 superblock (mount field)')
        try:
            for js, je in self._ext4_journal(f, off, sb):
                self._add(js, je, 'ext4 journal')
        except Exception:
            pass
        return 'ext4'

    def _ext4_journal(self, f, off: int, sb: bytes) -> list[tuple[int, int]]:
        if not (_u32(sb, 0x5c) & 0x0004):        # no FEATURE_COMPAT_HAS_JOURNAL
            return []
        incompat = _u32(sb, 0x60)
        if incompat & 0x0008:                    # external journal (JOURNAL_DEV)
            return []
        jinum = _u32(sb, 0xe0) or 8
        block_size = 1024 << _u32(sb, 0x18)
        inodes_per_group = _u32(sb, 0x28)
        inode_size = _u16(sb, 0x58) or 128
        first_data_block = _u32(sb, 0x14)
        is64 = bool(incompat & 0x0080)
        desc_size = (_u16(sb, 0xfe) or 32) if is64 else 32
        if not inodes_per_group:
            return []
        group, index = divmod(jinum - 1, inodes_per_group)

        gd_off = off + (first_data_block + 1) * block_size + group * desc_size
        f.seek(gd_off)
        gd = f.read(desc_size)
        itable = _u32(gd, 0x08)
        if is64 and desc_size >= 64:
            itable |= _u32(gd, 0x28) << 32

        f.seek(off + itable * block_size + index * inode_size)
        inode = f.read(max(inode_size, 128))
        i_flags = _u32(inode, 0x20)
        i_block = inode[0x28:0x28 + 60]
        if not (i_flags & 0x80000) or i_block[0:2] != b'\x0a\xf3':   # not extent-mapped
            return []

        ranges = []
        for phys, length in self._extents(f, i_block, block_size, off):
            s = off + phys * block_size
            ranges.append((s, s + length * block_size))
        return ranges

    def _extents(self, f, node: bytes, block_size: int, off: int) -> list[tuple[int, int]]:
        if node[0:2] != b'\x0a\xf3':              # extent header magic 0xF30A
            return []
        entries = _u16(node, 2)
        depth = _u16(node, 6)
        out = []
        for i in range(entries):
            e = node[12 + i * 12: 24 + i * 12]
            if len(e) < 12:
                break
            if depth == 0:                         # leaf: ee_block ee_len ee_start_hi ee_start_lo
                length = _u16(e, 4)
                if length > 32768:                 # uninitialised extent
                    length -= 32768
                phys = _u32(e, 8) | (_u16(e, 6) << 32)
                out.append((phys, length))
            else:                                  # index node: descend
                child = _u32(e, 4) | (_u16(e, 8) << 32)
                f.seek(off + child * block_size)
                out.extend(self._extents(f, f.read(block_size), block_size, off))
        return out

    def _add(self, start: int, end: int, label: str) -> None:
        if end > start:
            self.regions.append(Region(start, end, label, True))

    # ── classification ───────────────────────────────────────────────────────

    def classify(self, diffs) -> list[DiffRegion]:
        """Split each (start, end) difference into labelled pieces.

        Bytes covered by a known mount-volatile region are tagged benign with its
        label; everything else is tagged real and named by the partition/structure
        it lands in.
        """
        out: list[DiffRegion] = []
        for ds, de in diffs:
            out.extend(self._classify_one(ds, de))
        return out

    def _classify_one(self, ds: int, de: int) -> list[DiffRegion]:
        out: list[DiffRegion] = []
        pos = ds
        for r in self.regions:
            if r.end <= ds or r.start >= de:
                continue
            if r.start > pos:
                out.append(DiffRegion(pos, min(r.start, de), self._data_label(pos), False))
            cs, ce = max(r.start, ds), min(r.end, de)
            out.append(DiffRegion(cs, ce, r.label, True))
            pos = ce
            if pos >= de:
                break
        if pos < de:
            out.append(DiffRegion(pos, de, self._data_label(pos), False))
        return out

    def _data_label(self, off: int) -> str:
        for ps, pe, kind in self.partitions:
            if ps <= off < pe:
                rel = off - ps
                if kind == 'ext4':
                    return 'ext4 superblock' if 1024 <= rel < 2048 else 'root filesystem data'
                if kind == 'fat':
                    return 'FAT boot sector' if rel < 512 else 'boot partition data'
                return 'partition data'
        return 'MBR / partition gap'
