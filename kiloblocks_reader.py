#!/usr/bin/env python3
"""
Kiloblocks / Exploration — .dat world file reader/writer
Reverse-engineered from libkiloblocks.so (Android NDK C++)

=== FORMAT OVERVIEW ===

EXP1 PageCache format:
  - 1024-byte pages
  - Page 0:  global world header (EXP1 + EXPS magic)
  - Page 2:  BTree chunk coordinate index
  - Other pages: SaveChunk objects stored in StorageFile BTree

=== SAVECHUNK OBJECT FORMAT (per page) ===

  Bytes  0- 3:  int32   chunk_x   (world tile X of chunk origin)
  Bytes  4- 7:  int32   chunk_z   (world tile Z of chunk origin)
  Bytes  8-11:  uint32  flags/version
  Bytes 12-15:  int32   lz4_compressed_size
  Bytes 16+  :  bytes   LZ4-compressed tile data (LZ4_compress_default)

=== TILE DATA FORMAT (after LZ4 decompress, 98832 bytes total) ===

  First 98304 bytes = 32768 tiles × 3 bytes each:
    byte[0] = block_type   (0x00 = air)
    byte[1] = deform_lo
    byte[2] = deform_hi

  Layout: idx = x*(CHUNK_D*CHUNK_H) + z*CHUNK_H + y
    CHUNK_W=16 (X), CHUNK_H=128 (Y vertical), CHUNK_D=16 (Z)

  Last 528 bytes = trailer (all 0x40), preserved as-is.

=== COORDINATE MAPPING ===

  world_X = chunk_x + local_x      (0 <= local_x < 16)
  world_Z = chunk_z + local_z      (0 <= local_z < 16)
  game_Y  = tile_Y  + 22           (BASE_Y = 22, empirically confirmed)

=== KNOWN BLOCK TYPES ===

  0x00 = air          0x03 = stone (terrain fill)
  0x07 = terrain_07   0x08 = terrain_08
  0x0B = brick        0x02 = wood/planks
  0x06 = block_06     0x20 = terrain_marker

Usage:
  python3 kiloblocks_reader.py save.dat              # summary
  python3 kiloblocks_reader.py save.dat --blocks     # show placed blocks
  python3 kiloblocks_reader.py save.dat --diff other.dat
  python3 kiloblocks_reader.py save.dat --set X Y Z TYPE [--out out.dat]
  python3 kiloblocks_reader.py save.dat --json
"""

import struct, sys, json, ctypes
from pathlib import Path

PAGE_SIZE        = 1024
MAGIC            = b'EXP1'
MAGIC2           = b'EXPS'
CHUNK_W          = 16
CHUNK_H          = 128
CHUNK_D          = 16
TILE_BYTES       = 3
CHUNK_TILE_COUNT = CHUNK_W * CHUNK_H * CHUNK_D   # 32768
CHUNK_DATA_SIZE  = CHUNK_TILE_COUNT * TILE_BYTES  # 98304
CHUNK_TRAILER    = 528
CHUNK_DECOMP_SIZE = CHUNK_DATA_SIZE + CHUNK_TRAILER  # 98832

# Layout confirmed from CopyChunkToRegions/CopyRegionsToChunk decompilation:
#   chunk_data[z * H * W + y * W + x]   (ZYX order)
# Y=0 = bottom, Y=63 = surface (grass), Y=64+ = air (for flat/shore worlds)
# game_Y offset: tile_y=63 = grass surface. Game shows Y≈65 because
# game origin differs from tile origin by BASE_Y.
BASE_Y           = 22

TERRAIN_TYPES = {0: 'Earth', 1: 'Shore', 2: 'Flat', 3: 'Test'}
BLOCK_NAMES   = {
    0x00: 'air',       0x02: 'wood',    0x03: 'stone',
    0x06: 'block_06',  0x07: 'ter_07',  0x08: 'ter_08',
    0x0B: 'brick',     0x20: 'ter_20',
}

# -- LZ4 ------------------------------------------------------------------

def _load_lz4():
    try:
        lib = ctypes.CDLL('liblz4.so.1')
        lib.LZ4_decompress_safe.restype   = ctypes.c_int
        lib.LZ4_compress_default.restype  = ctypes.c_int
        return lib
    except OSError:
        pass
    try:
        import lz4.block as b
        return b
    except ImportError:
        return None

_LZ4 = _load_lz4()

def lz4_decompress(data: bytes, max_out: int = CHUNK_DECOMP_SIZE + 4096) -> bytes:
    if _LZ4 is None:
        raise RuntimeError("LZ4 not available — pip install lz4")
    if hasattr(_LZ4, 'LZ4_decompress_safe'):
        src = ctypes.create_string_buffer(bytes(data))
        dst = ctypes.create_string_buffer(max_out)
        r = _LZ4.LZ4_decompress_safe(src, dst, len(data), max_out)
        if r < 0:
            raise RuntimeError(f"LZ4_decompress_safe → {r}")
        return bytes(dst.raw[:r])
    return _LZ4.decompress(data, uncompressed_size=max_out)

def lz4_compress(data: bytes) -> bytes:
    if _LZ4 is None:
        raise RuntimeError("LZ4 not available — pip install lz4")
    if hasattr(_LZ4, 'LZ4_compress_default'):
        bound = len(data) + 1024
        src = ctypes.create_string_buffer(bytes(data))
        dst = ctypes.create_string_buffer(bound)
        r = _LZ4.LZ4_compress_default(src, dst, len(data), bound)
        if r <= 0:
            raise RuntimeError(f"LZ4_compress_default → {r}")
        return bytes(dst.raw[:r])
    return _LZ4.compress(data, store_size=False)

# -- Tile helpers ---------------------------------------------------------

def tile_idx(lx, ty, lz):
    """ZYX layout confirmed from CopyChunkToRegions: chunk[z*H*W + y*W + x]"""
    return lz * CHUNK_H * CHUNK_W + ty * CHUNK_W + lx

def idx_to_xyz(idx):
    z   = idx // (CHUNK_H * CHUNK_W)
    rem = idx %  (CHUNK_H * CHUNK_W)
    y   = rem // CHUNK_W
    x   = rem %  CHUNK_W
    return x, y, z

# -- Chunk ----------------------------------------------------------------

class Chunk:
    def __init__(self, cx, cz, tiles: bytearray, flags=0):
        self.chunk_x = cx
        self.chunk_z = cz
        self.flags   = flags
        assert len(tiles) == CHUNK_DECOMP_SIZE
        self.tiles = tiles

    def get(self, lx, ty, lz):
        return self.tiles[tile_idx(lx, ty, lz) * TILE_BYTES]

    def set(self, lx, ty, lz, btype, d0=0, d1=0):
        off = tile_idx(lx, ty, lz) * TILE_BYTES
        self.tiles[off], self.tiles[off+1], self.tiles[off+2] = btype&0xFF, d0&0xFF, d1&0xFF

    def get_world(self, wx, gy, wz):
        return self.get(wx - self.chunk_x, gy - BASE_Y, wz - self.chunk_z)

    def set_world(self, wx, gy, wz, btype):
        lx, ty, lz = wx - self.chunk_x, gy - BASE_Y, wz - self.chunk_z
        if not (0<=lx<CHUNK_W and 0<=ty<CHUNK_H and 0<=lz<CHUNK_D):
            raise ValueError(f"({wx},{gy},{wz}) outside chunk ({self.chunk_x},{self.chunk_z})")
        self.set(lx, ty, lz, btype)

    def contains(self, wx, wz):
        return self.chunk_x <= wx < self.chunk_x+CHUNK_W and \
               self.chunk_z <= wz < self.chunk_z+CHUNK_D

    def all_blocks(self):
        for idx in range(CHUNK_TILE_COUNT):
            bt = self.tiles[idx * TILE_BYTES]
            if bt:
                lx, ty, lz = idx_to_xyz(idx)
                yield self.chunk_x+lx, ty+BASE_Y, self.chunk_z+lz, bt

    def compress(self):
        return lz4_compress(bytes(self.tiles))

# -- World ----------------------------------------------------------------

class KiloblocksWorld:
    def __init__(self, path):
        self.path = Path(path)
        with open(path, 'rb') as f:
            self.raw = bytearray(f.read())
        assert self.raw[:4] == MAGIC
        self.page_size   = struct.unpack_from('<I', self.raw, 4)[0]
        self.total_pages = struct.unpack_from('<I', self.raw, 8)[0]
        self._chunks: dict  = {}   # (cx,cz) → Chunk
        self._pg_map: dict  = {}   # (cx,cz) → page number

    @property
    def world_info(self):
        d = self.raw
        h   = struct.unpack_from('<I', d, 0x30)[0]
        xm  = struct.unpack_from('<i', d, 0x34)[0]
        zm  = struct.unpack_from('<i', d, 0x38)[0]
        xs  = struct.unpack_from('<I', d, 0x3C)[0]
        zs  = struct.unpack_from('<I', d, 0x40)[0]
        return dict(
            total_pages=self.total_pages, file_size=len(self.raw),
            height=h, x_min=xm, x_max=xm+xs, z_min=zm, z_max=zm+zs,
            x_size=xs, z_size=zs,
            terrain=TERRAIN_TYPES.get(struct.unpack_from('<I',d,0x44)[0],'?'),
            seed=hex(struct.unpack_from('<Q',d,0x50)[0]),
            spawn_x=round(struct.unpack_from('<f',d,0x68)[0],4),
            spawn_y=round(struct.unpack_from('<f',d,0x6C)[0],4),
            spawn_z=round(struct.unpack_from('<f',d,0x70)[0],4),
        )

    @property
    def chunk_index(self):
        p2    = self.raw[2*PAGE_SIZE : 2*PAGE_SIZE+PAGE_SIZE]
        count = struct.unpack_from('<I', p2, 4)[0]
        out   = []
        for i in range(count):
            off = 8 + i*4
            if off+4 > PAGE_SIZE: break
            x = struct.unpack_from('<H', p2, off)[0] - 0x8000
            z = struct.unpack_from('<H', p2, off+2)[0] - 0x8000
            out.append((x, z))
        return out

    def _try_page(self, pg):
        off  = pg * PAGE_SIZE
        size = struct.unpack_from('<I', self.raw, off+4)[0]
        if size < 16: return None
        content = self.raw[off+8 : off+8+size]
        cx     = struct.unpack_from('<i', content, 0)[0]
        cz     = struct.unpack_from('<i', content, 4)[0]
        flags  = struct.unpack_from('<I', content, 8)[0]
        lz4_sz = struct.unpack_from('<i', content, 12)[0]
        if lz4_sz <= 0 or lz4_sz > size-16: return None
        try:
            tiles = bytearray(lz4_decompress(bytes(content[16:16+lz4_sz])))
        except Exception:
            return None
        if len(tiles) != CHUNK_DECOMP_SIZE: return None
        return Chunk(cx, cz, tiles, flags)

    def load_all_chunks(self):
        self._chunks.clear(); self._pg_map.clear()
        for pg in range(1, self.total_pages):
            c = self._try_page(pg)
            if c:
                key = (c.chunk_x, c.chunk_z)
                self._chunks[key] = c
                self._pg_map[key] = pg

    def find_chunk(self, wx, wz):
        if not self._chunks: self.load_all_chunks()
        for c in self._chunks.values():
            if c.contains(wx, wz): return c
        return None

    def get_block(self, wx, gy, wz):
        c = self.find_chunk(wx, wz)
        if c is None: return 0
        ty = gy - BASE_Y
        if not (0 <= ty < CHUNK_H): return 0
        return c.get(wx-c.chunk_x, ty, wz-c.chunk_z)

    def set_block(self, wx, gy, wz, btype):
        c = self.find_chunk(wx, wz)
        if c is None:
            raise ValueError(f"No chunk for ({wx},{wz})")
        c.set_world(wx, gy, wz, btype)

    def all_blocks(self):
        if not self._chunks: self.load_all_chunks()
        for c in self._chunks.values():
            yield from c.all_blocks()

    def diff(self, other):
        if not self._chunks:  self.load_all_chunks()
        if not other._chunks: other.load_all_chunks()
        for key in sorted(set(self._chunks) | set(other._chunks)):
            ca = self._chunks.get(key)
            cb = other._chunks.get(key)
            if ca is None or cb is None: continue
            for idx in range(CHUNK_TILE_COUNT):
                ta = ca.tiles[idx*TILE_BYTES]
                tb = cb.tiles[idx*TILE_BYTES]
                if ta != tb:
                    lx, ty, lz = idx_to_xyz(idx)
                    yield key[0]+lx, ty+BASE_Y, key[1]+lz, ta, tb

    def save(self, path=None):
        if not self._chunks: raise RuntimeError("No chunks loaded")
        for key, chunk in self._chunks.items():
            pg = self._pg_map.get(key)
            if pg is None: raise RuntimeError(f"No page for chunk {key}")
            compressed = chunk.compress()
            buf = struct.pack('<iiIi', chunk.chunk_x, chunk.chunk_z,
                              chunk.flags, len(compressed)) + compressed
            if len(buf) > PAGE_SIZE - 8:
                raise RuntimeError(f"Chunk {key} too large for single page")
            off = pg * PAGE_SIZE
            struct.pack_into('<i', self.raw, off,   -1)
            struct.pack_into('<I', self.raw, off+4, len(buf))
            self.raw[off+8 : off+PAGE_SIZE] = b'\x00' * (PAGE_SIZE-8)
            self.raw[off+8 : off+8+len(buf)] = buf
        out = Path(path) if path else self.path
        with open(out, 'wb') as f:
            f.write(self.raw)
        print(f"Saved → {out}")

    def print_summary(self):
        wi = self.world_info
        ci = self.chunk_index
        if not self._chunks: self.load_all_chunks()
        print("=" * 60)
        print("  Kiloblocks/Exploration World")
        print("=" * 60)
        print(f"  File:    {self.path.name}  ({wi['file_size']:,} bytes)")
        print(f"  Pages:   {wi['total_pages']} × {PAGE_SIZE}")
        print(f"  World:   X {wi['x_min']}..{wi['x_max']},  "
              f"Z {wi['z_min']}..{wi['z_max']},  H {wi['height']}")
        print(f"  Terrain: {wi['terrain']}")
        print(f"  Seed:    {wi['seed']}")
        print(f"  Spawn:   ({wi['spawn_x']}, {wi['spawn_y']}, {wi['spawn_z']})")
        print()
        print(f"  Chunk index entries: {len(ci)}")
        if ci:
            xs=[c[0] for c in ci]; zs=[c[1] for c in ci]
            print(f"    X: {min(xs)} .. {max(xs)},  Z: {min(zs)} .. {max(zs)}")
        print()
        print(f"  Loaded chunks: {len(self._chunks)}")
        for (cx,cz) in sorted(self._chunks):
            c = self._chunks[(cx,cz)]
            na = sum(1 for i in range(CHUNK_TILE_COUNT) if c.tiles[i*TILE_BYTES])
            print(f"    ({cx:4d}, {cz:4d})  {na} non-air tiles")

# -- CLI ------------------------------------------------------------------

_TERRAIN_TYPES = {0x00,0x03,0x07,0x08,0x20,0x40}

def _bname(t): return BLOCK_NAMES.get(t, f'0x{t:02X}')

def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h','--help'):
        print(__doc__); sys.exit(0)

    path  = args[0]
    world = KiloblocksWorld(path)
    world.load_all_chunks()

    if '--json' in args:
        out = dict(world=world.world_info, chunk_index=world.chunk_index,
                   chunks=[{'cx':cx,'cz':cz,
                             'non_air':sum(1 for i in range(CHUNK_TILE_COUNT)
                                          if world._chunks[(cx,cz)].tiles[i*TILE_BYTES])}
                            for cx,cz in sorted(world._chunks)])
        print(json.dumps(out, indent=2))

    elif '--blocks' in args:
        skip_terrain = '--all' not in args
        print(f"{'X':>8} {'Y':>7} {'Z':>8}  block")
        print("-"*38)
        n = 0
        for wx,gy,wz,bt in sorted(world.all_blocks()):
            if skip_terrain and bt in _TERRAIN_TYPES: continue
            print(f"{wx:8d} {gy:7d} {wz:8d}  {_bname(bt)}")
            n += 1
        print(f"\n({n} blocks)")

    elif '--diff' in args:
        idx2 = args.index('--diff')
        if idx2+1 >= len(args): print("--diff needs a second file"); sys.exit(1)
        other = KiloblocksWorld(args[idx2+1])
        other.load_all_chunks()
        print(f"{'X':>8} {'Y':>7} {'Z':>8}  before → after")
        print("-"*50)
        n = 0
        for wx,gy,wz,ta,tb in world.diff(other):
            print(f"{wx:8d} {gy:7d} {wz:8d}  {_bname(ta)} → {_bname(tb)}")
            n += 1
        print(f"\n({n} changes)")

    elif '--set' in args:
        idx2 = args.index('--set')
        if idx2+4 >= len(args):
            print("Usage: --set X Y Z TYPE  (TYPE decimal or 0xHH)"); sys.exit(1)
        wx = int(args[idx2+1])
        gy = int(args[idx2+2])
        wz = int(args[idx2+3])
        braw = args[idx2+4]
        bt = int(braw,16) if braw.lower().startswith('0x') else int(braw)
        old = world.get_block(wx,gy,wz)
        world.set_block(wx,gy,wz,bt)
        print(f"({wx},{gy},{wz}): {_bname(old)} → {_bname(bt)}")
        out = None
        if '--out' in args:
            out = args[args.index('--out')+1]
        else:
            p = Path(path)
            out = str(p.with_stem(p.stem+'_modified'))
        world.save(out)

    else:
        world.print_summary()

if __name__ == '__main__':
    main()
