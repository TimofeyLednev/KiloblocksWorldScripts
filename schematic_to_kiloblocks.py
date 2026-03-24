#!/usr/bin/env python3
"""
schematic_to_kiloblocks.py
Конвертер Minecraft .schematic → Kiloblocks/Exploration .dat

Зависимости:
    pip install lz4 nbtlib numpy

Использование:
    python3 schematic_to_kiloblocks.py build.schematic world.dat
    python3 schematic_to_kiloblocks.py build.schematic world.dat -o out.dat
    python3 schematic_to_kiloblocks.py build.schematic world.dat --ox -10 --oz 5 --oy 64
    python3 schematic_to_kiloblocks.py build.schematic world.dat --list-blocks

Координаты вставки:
    --ox   смещение X в мире Kiloblocks (default: авто-центр)
    --oz   смещение Z в мире Kiloblocks (default: авто-центр)
    --oy   тайловый Y для НИЖНЕГО слоя schematic (default: 64 = поверхность)
           Kiloblocks: Y=0 дно, Y=63 поверхность земли, Y=64 первый воздух
    При --oy 64 постройка стоит прямо на земле.
    При --oy 60 нижние 4 блока уйдут под землю.

Важно:
    Перед вставкой пройдись по всему миру в игре чтобы все чанки сохранились.
    Блоки в незагруженные чанки пропускаются.
"""

import struct, sys, os, ctypes, argparse
import numpy as np

try:
    import nbtlib
except ImportError:
    sys.exit("pip install nbtlib")

try:
    import lz4.block as _lz4
    def lz4_decomp(data, maxout=200000):
        return _lz4.decompress(data, uncompressed_size=maxout)
    def lz4_comp(data):
        return _lz4.compress(data, store_size=False)
except ImportError:
    try:
        _lib = ctypes.CDLL('liblz4.so.1')
        _lib.LZ4_decompress_safe.restype  = ctypes.c_int
        _lib.LZ4_compress_default.restype = ctypes.c_int
        def lz4_decomp(data, maxout=200000):
            src = ctypes.create_string_buffer(bytes(data))
            dst = ctypes.create_string_buffer(maxout)
            r = _lib.LZ4_decompress_safe(src, dst, len(data), maxout)
            if r < 0: raise RuntimeError(f"LZ4 decomp {r}")
            return bytes(dst.raw[:r])
        def lz4_comp(data):
            bound = len(data) + 1024
            src = ctypes.create_string_buffer(bytes(data))
            dst = ctypes.create_string_buffer(bound)
            r = _lib.LZ4_compress_default(src, dst, len(data), bound)
            if r <= 0: raise RuntimeError(f"LZ4 comp {r}")
            return bytes(dst.raw[:r])
    except OSError:
        sys.exit("pip install lz4")

# ── константы ────────────────────────────────────────────────────────────────

PAGE_SIZE         = 1024
CW, CH, CD        = 16, 128, 16
CHUNK_TILES       = CW * CH * CD        # 32768
CHUNK_DATA_BYTES  = CHUNK_TILES * 3     # 98304  (3 байта / тайл)
CHUNK_TRAILER     = 528                 # 0x40 padding
CHUNK_DECOMP_SIZE = CHUNK_DATA_BYTES + CHUNK_TRAILER  # 98832

# ── маппинг MC → Kiloblocks Index ────────────────────────────────────────────

MC_TO_KB = {
    0:   0,
    1:   1,    # Stone          → Stone
    2:   8,    # Grass Block    → Grass
    3:   7,    # Dirt           → Dirt
    4:   23,   # Cobblestone    → Cobblestone
    5:   6,    # Oak Planks     → Planks 1
    7:   1,    # Bedrock        → Stone
    8:   5,    # Flowing Water  → Water
    9:   5,    # Still Water    → Water
    10:  1,    # Lava           → Stone
    11:  1,
    12:  24,   # Sand           → Sand
    13:  25,   # Gravel         → Gravel
    14:  63,   # Gold Ore       → Gold
    15:  62,   # Iron Ore       → Iron
    16:  1,    # Coal Ore       → Stone
    17:  4,    # Oak Log        → Wood 1
    18:  10,   # Leaves         → Leaves 1
    20:  11,   # Glass          → Glass
    24:  95,   # Sandstone      → Sandstone
    26:  64,   # Bed            → Bed 1
    31:  76,   # Tall Grass     → Tall Grass
    32:  77,   # Dead Bush      → Dead Bush
    35:  46,   # Wool (white default, overridden below)
    37:  13,   # Dandelion      → Flower 1
    38:  14,   # Poppy          → Flower 2
    39:  26,   # Brown Mushroom → Mushroom 1
    40:  27,   # Red Mushroom   → Mushroom 2
    41:  63,   # Gold Block     → Gold
    42:  62,   # Iron Block     → Iron
    44:  12,   # Stone Slab     → Stone Slab
    45:  2,    # Bricks         → Brick
    47:  33,   # Bookshelf      → Bookshelf
    48:  23,   # Mossy Cobblestone → Cobblestone
    49:  81,   # Obsidian       → Obsidian
    50:  18,   # Torch          → Active Sconce
    53:  72,   # Oak Stairs     → Planks Stairs 1
    54:  74,   # Chest          → Drawers 1
    57:  62,   # Diamond Block  → Iron
    58:  38,   # Crafting Table → Box
    60:  7,    # Farmland       → Dirt
    61:  39,   # Furnace        → Stove
    62:  39,   # Lit Furnace    → Stove
    64:  67,   # Wood Door      → Wood Door 1
    65:  15,   # Ladder         → Ladder
    67:  73,   # Cobblestone Stairs
    71:  79,   # Iron Door      → Iron Door 1
    78:  37,   # Snow Layer     → Snow Layer
    79:  70,   # Ice            → Ice
    80:  41,   # Snow Block     → Snow
    82:  28,   # Clay           → Clay
    83:  29,   # Sugar Cane     → Cane
    85:  66,   # Fence          → Fence
    86:  87,   # Pumpkin        → Pumpkin
    87:  1,    # Netherrack     → Stone
    89:  45,   # Glowstone      → Active Lamp
    95:  11,   # Stained Glass  → Glass
    98:  101,  # Stone Bricks   → Stone Brick
    102: 11,   # Glass Pane     → Glass
    103: 86,   # Melon Block    → Melon
    107: 78,   # Fence Gate
    108: 83,   # Brick Stairs
    109: 73,   # Stone Brick Stairs
    112: 92,   # Nether Brick   → Black Cobblestone
    128: 97,   # Sandstone Stairs
    155: 63,   # Quartz Block   → Gold
    159: 46,   # Stained Clay   → White Fabric
    161: 10,   # Acacia Leaves  → Leaves 1
    162: 4,    # Acacia Log     → Wood 1
    170: 88,   # Hay Bale       → Thatch
    171: 46,   # Carpet         → White Fabric
    172: 7,    # Hardened Clay  → Dirt
    173: 81,   # Coal Block     → Obsidian
    174: 70,   # Packed Ice     → Ice
}

WOOL_TO_KB = {
    0: 46, 1: 50, 2: 58, 3: 56, 4: 52, 5: 53,
    6: 60, 7: 48, 8: 47, 9: 55, 10: 57, 11: 57,
    12: 61, 13: 54, 14: 49, 15: 49,
}

DEFAULT_KB = 1


def mc_to_kb(mc_id, data_val=0):
    if mc_id == 0:
        return 0
    if mc_id == 35:
        return WOOL_TO_KB.get(data_val & 0xF, 46)
    return MC_TO_KB.get(mc_id, DEFAULT_KB)

# ── чтение schematic ──────────────────────────────────────────────────────────

def read_schematic(path):
    nbt = nbtlib.load(path, gzipped=True)
    W = int(nbt['Width'])
    H = int(nbt['Height'])
    L = int(nbt['Length'])
    blocks = bytes(nbt['Blocks'])
    data   = bytes(nbt['Data']) if 'Data' in nbt else bytes(W * H * L)
    # schematic: blocks[(y * L + z) * W + x]
    return W, H, L, blocks, data

# ── чтение .dat ───────────────────────────────────────────────────────────────

def load_dat(path):
    raw = open(path, 'rb').read()
    if raw[:4] != b'EXP1':
        sys.exit(f"Не EXP1 файл: {path}")
    total  = struct.unpack_from('<I', raw, 8)[0]
    chunks = {}
    for pg in range(1, total):
        off = pg * PAGE_SIZE
        sz  = struct.unpack_from('<I', raw, off + 4)[0]
        if sz < 16: continue
        content = raw[off + 8 : off + 8 + sz]
        cx      = struct.unpack_from('<i', content, 0)[0]
        cz      = struct.unpack_from('<i', content, 4)[0]
        flags   = struct.unpack_from('<I', content, 8)[0]
        lz4_sz  = struct.unpack_from('<i', content, 12)[0]
        if lz4_sz <= 0 or lz4_sz > sz - 16: continue
        try:
            decomp = lz4_decomp(bytes(content[16 : 16 + lz4_sz]))
        except Exception:
            continue
        if len(decomp) < CHUNK_DATA_BYTES: continue
        tiles   = bytearray(decomp[:CHUNK_DATA_BYTES])
        trailer = decomp[CHUNK_DATA_BYTES:]   # сохраняем трейлер
        chunks[(cx, cz)] = {
            'page': pg, 'tiles': tiles,
            'flags': flags, 'trailer': trailer,
            'dirty': False,
        }
    return bytearray(raw), chunks

# ── запись тайла (ZYX layout) ─────────────────────────────────────────────────

def set_tile(tiles, lx, ly, lz, kb_idx):
    if not (0 <= lx < CW and 0 <= ly < CH and 0 <= lz < CD):
        return
    idx = lz * CH * CW + ly * CW + lx
    tiles[idx] = kb_idx   # byte[0] = block type

# ── конвертация ───────────────────────────────────────────────────────────────

def convert(sch_W, sch_H, sch_L, sch_blocks, sch_data,
            chunks, offset_x, offset_y, offset_z):
    """
    Вставляет schematic в чанки Kiloblocks.
    offset_y = тайловый Y нижнего слоя schematic (sch_y=0 → tile_y=offset_y).
    Любой блок заменяет то что было (включая землю).
    """
    placed  = 0
    skipped = 0
    missing = set()

    for sch_y in range(sch_H):
        tile_y = offset_y + sch_y
        if tile_y < 0 or tile_y >= CH:
            continue                       # за пределами высоты мира
        for sch_z in range(sch_L):
            for sch_x in range(sch_W):
                sch_idx = (sch_y * sch_L + sch_z) * sch_W + sch_x
                mc_id   = sch_blocks[sch_idx] & 0xFF
                mc_data = sch_data[sch_idx]   & 0x0F
                kb_idx  = mc_to_kb(mc_id, mc_data)

                wx = sch_x + offset_x
                wz = sch_z + offset_z

                # Чанк содержащий (wx, wz)
                cx = (wx // CW) * CW if wx >= 0 else ((wx - CW + 1) // CW) * CW
                cz = (wz // CD) * CD if wz >= 0 else ((wz - CD + 1) // CD) * CD

                if (cx, cz) not in chunks:
                    missing.add((cx, cz))
                    skipped += 1
                    continue

                chunk = chunks[(cx, cz)]
                lx = wx - cx
                lz = wz - cz
                set_tile(chunk['tiles'], lx, tile_y, lz, kb_idx)
                chunk['dirty'] = True
                placed += 1

    return placed, skipped, missing

# ── запись .dat ───────────────────────────────────────────────────────────────

def save_dat(raw, chunks, out_path):
    for (cx, cz), chunk in chunks.items():
        if not chunk['dirty']:
            continue
        pg      = chunk['page']
        tiles   = chunk['tiles']
        trailer = chunk.get('trailer', bytes(CHUNK_TRAILER))
        # Восстанавливаем полный буфер (данные + трейлер)
        full       = bytes(tiles) + bytes(trailer)
        compressed = lz4_comp(full)
        buf = struct.pack('<iiIi', cx, cz, chunk['flags'], len(compressed)) + compressed
        if len(buf) > PAGE_SIZE - 8:
            print(f"  SKIP: чанк ({cx},{cz}) не влезает в страницу ({len(buf)} байт)")
            continue
        off = pg * PAGE_SIZE
        struct.pack_into('<i', raw, off,     -1)
        struct.pack_into('<I', raw, off + 4, len(buf))
        raw[off + 8 : off + PAGE_SIZE] = b'\x00' * (PAGE_SIZE - 8)
        raw[off + 8 : off + 8 + len(buf)] = buf
    with open(out_path, 'wb') as f:
        f.write(raw)

# ── main ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description='Minecraft .schematic → Kiloblocks .dat')
    p.add_argument('schematic',        help='Входной .schematic')
    p.add_argument('world',            help='Входной .dat мир Kiloblocks')
    p.add_argument('-o', '--output',   help='Выходной .dat (default: world_out.dat)')
    p.add_argument('--ox', type=int,   default=None,
                   help='Смещение X (default: авто-центр по загруженным чанкам)')
    p.add_argument('--oz', type=int,   default=None,
                   help='Смещение Z (default: авто-центр)')
    p.add_argument('--oy', type=int,   default=64,
                   help='Тайловый Y нижнего слоя schematic (default=64 = поверхность)')
    p.add_argument('--list-blocks',    action='store_true',
                   help='Показать MC блоки в schematic')
    args = p.parse_args()

    for f in [args.schematic, args.world]:
        if not os.path.isfile(f):
            sys.exit(f"Файл не найден: {f}")

    out_path = args.output or (os.path.splitext(args.world)[0] + '_out.dat')

    # Читаем schematic
    print(f"Читаю {args.schematic} ...")
    sch_W, sch_H, sch_L, sch_blocks, sch_data = read_schematic(args.schematic)
    print(f"  Schematic: {sch_W} (X) × {sch_H} (Y) × {sch_L} (Z)")

    if args.list_blocks:
        from collections import Counter
        cnt = Counter(sch_blocks[i] & 0xFF for i in range(len(sch_blocks))
                      if sch_blocks[i])
        print("  MC блоки:")
        for mc, n in cnt.most_common(20):
            print(f"    MC {mc:3d} → KB {mc_to_kb(mc):3d}   {n:,}")
        print()

    # Читаем мир
    print(f"Читаю {args.world} ...")
    raw, chunks = load_dat(args.world)
    print(f"  Чанков: {len(chunks)}")

    if not chunks:
        sys.exit("В .dat нет ни одного чанка. Походи по миру в игре и сохранись.")

    cxs = sorted(set(k[0] for k in chunks))
    czs = sorted(set(k[1] for k in chunks))
    wx_min, wx_max = min(cxs), max(cxs) + CW - 1
    wz_min, wz_max = min(czs), max(czs) + CD - 1
    print(f"  Покрытие: X {wx_min}..{wx_max},  Z {wz_min}..{wz_max}")

    # Смещение: авто-центр если не задано
    if args.ox is None:
        world_cx = (wx_min + wx_max + 1) // 2
        args.ox = world_cx - sch_W // 2
    if args.oz is None:
        world_cz = (wz_min + wz_max + 1) // 2
        args.oz = world_cz - sch_L // 2

    tile_y_bottom = args.oy
    tile_y_top    = args.oy + sch_H - 1

    print(f"\n  Вставка:")
    print(f"    X: {args.ox} .. {args.ox + sch_W - 1}")
    print(f"    Y: {tile_y_bottom} .. {tile_y_top}  (tile Y)")
    print(f"    Z: {args.oz} .. {args.oz + sch_L - 1}")

    if tile_y_top >= CH:
        print(f"  ПРЕДУПРЕЖДЕНИЕ: верхние {tile_y_top - CH + 1} слоёв обрежутся (max Y={CH-1})")
    if tile_y_bottom < 0:
        print(f"  ПРЕДУПРЕЖДЕНИЕ: нижние {-tile_y_bottom} слоёв обрежутся (min Y=0)")

    # Конвертируем
    print("\nЗаписываю блоки ...")
    placed, skipped, missing = convert(
        sch_W, sch_H, sch_L, sch_blocks, sch_data,
        chunks, args.ox, tile_y_bottom, args.oz,
    )
    print(f"  Записано:  {placed:,}")
    if skipped:
        print(f"  Пропущено: {skipped:,}  (чанки не загружены в игре)")
        if missing:
            shown = list(sorted(missing))[:6]
            print(f"  Примеры чанков без данных: {shown}")

    # Сохраняем
    print(f"\nСохраняю {out_path} ...")
    save_dat(raw, chunks, out_path)
    print(f"  Готово! {os.path.getsize(out_path):,} байт")
    print(f"\n  Скопируй {os.path.basename(out_path)} в папку сохранений Kiloblocks,")
    print(f"  переименовав в оригинальное имя (например save1.dat).")


if __name__ == '__main__':
    main()
