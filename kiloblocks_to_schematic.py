#!/usr/bin/env python3
"""
kiloblocks_to_schematic.py
Конвертер Kiloblocks/Exploration .dat → Minecraft .schematic

Зависимости:
    pip install lz4 nbtlib numpy

Использование:
    python3 kiloblocks_to_schematic.py save.dat
    python3 kiloblocks_to_schematic.py save.dat -o world.schematic
    python3 kiloblocks_to_schematic.py save.dat --surface
    python3 kiloblocks_to_schematic.py save.dat --ymin 0 --ymax 127
    python3 kiloblocks_to_schematic.py save.dat --list-blocks
"""

import struct, sys, os, ctypes, argparse
import numpy as np

# ── зависимости ──────────────────────────────────────────────────────────────

try:
    import nbtlib
except ImportError:
    sys.exit("Установи nbtlib:  pip install nbtlib")

try:
    import lz4.block as _lz4
    def lz4_decomp(data, maxout=200000):
        return _lz4.decompress(data, uncompressed_size=maxout)
except ImportError:
    try:
        _lib = ctypes.CDLL('liblz4.so.1')
        _lib.LZ4_decompress_safe.restype = ctypes.c_int
        def lz4_decomp(data, maxout=200000):
            src = ctypes.create_string_buffer(bytes(data))
            dst = ctypes.create_string_buffer(maxout)
            r = _lib.LZ4_decompress_safe(src, dst, len(data), maxout)
            if r < 0: raise RuntimeError(f"LZ4 error {r}")
            return bytes(dst.raw[:r])
    except OSError:
        sys.exit("Установи lz4:  pip install lz4")

# ── константы ────────────────────────────────────────────────────────────────

PAGE_SIZE   = 1024
CW, CH, CD  = 16, 128, 16   # chunk width / height / depth
CHUNK_TILES = CW * CH * CD  # 32768

# ── таблица блоков ───────────────────────────────────────────────────────────
#
# Ключ = S-Index из blocks.txt (= то что хранится в .dat)
# Значение = Minecraft legacy block ID (для schematic Alpha)
#
# Таблица Kiloblocks S-Index → название:
#   0=Stone  1=Brick  2=Granite  3=Wood1  4=Planks1  5=Dirt
#   6=Leaves1  7=Glass  8=Stone Slab  9=Flower1  10=Flower2
#   11=Ladder  12=Wood2  13=Active Sconce  14=Grass  15=Leaves2
#   16=Planks Slab1  17=Granite Slab  18=Cobblestone  19=Sand
#   20=Gravel  21=Mushroom1  22=Mushroom2  23=Clay  24=Cane
#   25=Sand Slope  26=Cobblestone Slab  29=Snow Layer  30=Box
#   31=Stove  32=Snow  33=Drawers1  34=Drawers2  35=Active Lamp
#   45-53=Fabric colors  63-71=More fabrics/Iron/Gold
#   70=Iron  71=Gold  74=Fence  77=Ice  79=Planks Stairs1
#   80=Cobblestone Stairs  81=Tall Grass  82=Dead Bush  86=Obsidian
#   88=Thatch  90=Brick Slab  91=Leaves3  96=Black Cobblestone
#   99=Sandstone  101=Stone Brick  104=Planks2 ...

# Ключ = Index из blocks.txt (= raw byte в .dat файле, первый столбец)
# Это НЕ S-Index!  raw=3 → Index=3 → Granite (terrain stone)
#                  raw=7 → Index=7 → Dirt
#                  raw=8 → Index=8 → Grass
KB_NAMES = {
    0:  "Air",
    1:  "Stone",          2:  "Brick",           3:  "Granite",
    4:  "Wood 1",         5:  "Water",            6:  "Planks 1",
    7:  "Dirt",           8:  "Grass",            9:  "Leaves 1 Opaque",
    10: "Leaves 1",       11: "Glass",            12: "Stone Slab",
    13: "Flower 1",       14: "Flower 2",         15: "Ladder",
    16: "Wood 2",         17: "Active Sconce",    18: "Leaves 2 Opaque",
    19: "Leaves 2",       20: "Planks Slab 1",    21: "Granite Slab",
    22: "Cobblestone",    23: "Sand",             24: "Gravel",
    25: "Mushroom 1",     26: "Mushroom 2",       27: "Clay",
    28: "Cane",           29: "Slabs",            30: "Slab",
    31: "Cobblestone Slab", 32: "Bookshelf",      33: "TV Set",
    34: "Active TV Set",  35: "Snow Block",       36: "Snow Layer",
    37: "Box",            38: "Stove",            39: "Active Stove",
    40: "Snow",           41: "Lying Wood 1",     42: "Lying Wood 2",
    43: "Lamp",           44: "Active Lamp",
    45: "White Fabric",   46: "Light Gray Fabric", 47: "Gray Fabric",
    48: "Black Fabric",   49: "Red Fabric",        50: "Orange Fabric",
    51: "Yellow Fabric",  52: "Lime Fabric",       53: "Green Fabric",
    54: "Cyan Fabric",    55: "Light Blue Fabric", 56: "Blue Fabric",
    57: "Purple Fabric",  58: "Magenta Fabric",    59: "Pink Fabric",
    60: "Brown Fabric",   61: "Iron",              62: "Gold",
    63: "Bed 1",          64: "Bed 2",             65: "Fence",
    66: "Wood Door 1",    67: "Small Lamp",        68: "Active Small Lamp",
    69: "Ice",            70: "Wood Door 2",       71: "Planks Stairs 1",
    72: "Cobblestone Stairs", 73: "Drawers 1",     74: "Drawers 2",
    75: "Tall Grass",     76: "Dead Bush",         77: "Fence Gate",
    78: "Iron Door 1",    79: "Roof Window",       80: "Obsidian",
    81: "Iron Door 2",    82: "Brick Slab",        83: "Brick Stairs",
    84: "Leaves 3",       85: "Melon",             86: "Pumpkin",
    87: "Thatch",         88: "Lying Thatch",      89: "Tabletop 1",
    90: "Tabletop 2",     91: "Black Cobblestone", 92: "Black Cobblestone Slab",
    93: "Black Cobblestone Stairs", 94: "Sandstone", 95: "Sandstone Slab",
    96: "Sandstone Stairs", 97: "Sandstone Brick", 98: "Sandstone Brick Slab",
    99: "Sandstone Brick Stairs", 100: "Stone Brick", 101: "Stone Brick Slab",
    102: "Stone Brick Stairs", 103: "Planks 2",    104: "Planks Slab 2",
    105: "Planks Stairs 2", 106: "Planks 3",       107: "Planks Slab 3",
    108: "Planks Stairs 3", 109: "Planks 4",       110: "Planks Slab 4",
    111: "Planks Stairs 4", 112: "Stone Slope",    113: "Brick Slope",
    114: "Planks Slope 1",  115: "Wood Slope 1",   116: "Wood Slope 2",
    117: "Thatch Slope",    118: "Leaves 1 Slope", 119: "Leaves 2 Slope",
    120: "Leaves 3 Slope",  121: "Dirt Slope",     122: "Grass Slope",
    123: "Bookshelf Slab",  124: "White Fabric Slope", 125: "Light Gray Fabric Slope",
    126: "Gray Fabric Slope", 127: "Black Fabric Slope", 128: "Red Fabric Slope",
    129: "Orange Fabric Slope", 130: "Yellow Fabric Slope", 131: "Lime Fabric Slope",
    132: "Green Fabric Slope",  133: "Cyan Fabric Slope",  134: "Light Blue Fabric Slope",
    135: "Blue Fabric Slope",   136: "Purple Fabric Slope", 137: "Magenta Fabric Slope",
    138: "Pink Fabric Slope",   139: "Brown Fabric Slope",  140: "Iron Slope",
    141: "Gold Slope",          142: "Sand Slope",          143: "TV Panel",
    144: "Active TV Panel",
}

# Kiloblocks Index → Minecraft legacy block ID
BLOCK_MAP = {
    # terrain
    1:   1,    # Stone          → Stone
    3:   1,    # Granite        → Stone
    7:   3,    # Dirt           → Dirt
    8:   2,    # Grass          → Grass Block
    23:  4,    # Cobblestone    → Cobblestone
    24:  12,   # Sand           → Sand
    25:  13,   # Gravel         → Gravel
    28:  82,   # Clay           → Clay
    41:  80,   # Snow           → Snow Block
    37:  78,   # Snow Layer     → Snow Layer
    95:  24,   # Sandstone      → Sandstone
    101: 98,   # Stone Brick    → Stone Bricks
    92:  4,    # Black Cobblestone → Cobblestone
    5:   9,    # Water          → Still Water

    # wood / planks
    4:   17,   # Wood 1         → Oak Log
    16:  17,   # Wood 2         → Oak Log
    6:   5,    # Planks 1       → Oak Planks
    104: 5,    # Planks 2       → Oak Planks
    107: 5,    # Planks 3       → Oak Planks
    110: 5,    # Planks 4       → Oak Planks
    88:  5,    # Thatch         → Oak Planks

    # building blocks
    2:   45,   # Brick          → Bricks
    22:  44,   # Granite Slab   → Stone Slab
    32:  44,   # Cobblestone Slab → Stone Slab
    12:  44,   # Stone Slab     → Stone Slab
    83:  44,   # Brick Slab     → Stone Slab
    93:  44,   # Black Cobblestone Slab → Stone Slab
    98:  24,   # Sandstone Brick → Sandstone
    94:  67,   # Black Cobblestone Stairs → Cobblestone Stairs
    72:  53,   # Planks Stairs 1 → Wood Stairs
    73:  67,   # Cobblestone Stairs
    84:  108,  # Brick Stairs
    97:  67,   # Sandstone Stairs

    # nature
    10:  18,   # Leaves 1       → Leaves
    20:  18,   # Leaves 2       → Leaves
    85:  18,   # Leaves 3       → Leaves
    29:  83,   # Cane           → Sugar Cane
    13:  37,   # Flower 1       → Dandelion
    14:  38,   # Flower 2       → Rose/Poppy
    76:  31,   # Tall Grass     → Tall Grass
    77:  32,   # Dead Bush      → Dead Bush
    26:  40,   # Mushroom 1     → Brown Mushroom
    27:  39,   # Mushroom 2     → Red Mushroom

    # glass & ice
    11:  20,   # Glass          → Glass
    70:  79,   # Ice            → Ice
    81:  49,   # Obsidian       → Obsidian

    # furniture / misc
    15:  65,   # Ladder         → Ladder
    66:  85,   # Fence          → Oak Fence
    78:  107,  # Fence Gate     → Fence Gate
    67:  64,   # Wood Door 1    → Wood Door
    71:  64,   # Wood Door 2    → Wood Door
    79:  71,   # Iron Door 1    → Iron Door
    82:  71,   # Iron Door 2    → Iron Door
    38:  58,   # Box            → Crafting Table
    39:  61,   # Stove          → Furnace
    74:  54,   # Drawers 1      → Chest
    75:  54,   # Drawers 2      → Chest

    # fabrics → wool
    46:  35,   # White Fabric   → Wool
    47:  35,   # Light Gray Fabric
    48:  35,   # Gray Fabric
    49:  35,   # Black Fabric
    50:  35,   # Red Fabric
    51:  35,   # Orange Fabric
    52:  35,   # Yellow Fabric
    53:  35,   # Lime Fabric
    54:  35,   # Green Fabric
    55:  35,   # Cyan Fabric
    56:  35,   # Light Blue Fabric
    57:  35,   # Blue Fabric
    58:  35,   # Purple Fabric
    59:  35,   # Magenta Fabric
    60:  35,   # Pink Fabric
    61:  35,   # Brown Fabric
    62:  42,   # Iron           → Iron Block
    63:  41,   # Gold           → Gold Block
}

DEFAULT_MC = 1  # всё остальное непустое → Stone


def kb_name(idx):
    return KB_NAMES.get(idx, f"Unknown({idx})")


def kb_to_mc(idx):
    if idx == 0:
        return 0  # air
    return BLOCK_MAP.get(idx, DEFAULT_MC)

# ── чтение .dat ──────────────────────────────────────────────────────────────

def load_chunks(path):
    data = open(path, 'rb').read()
    if data[:4] != b'EXP1':
        sys.exit(f"Не EXP1 файл: {path}")
    total_pages = struct.unpack_from('<I', data, 8)[0]
    chunks = {}
    for pg in range(1, total_pages):
        off = pg * PAGE_SIZE
        sz = struct.unpack_from('<I', data, off + 4)[0]
        if sz < 16:
            continue
        content = data[off + 8 : off + 8 + sz]
        cx     = struct.unpack_from('<i', content, 0)[0]
        cz     = struct.unpack_from('<i', content, 4)[0]
        lz4_sz = struct.unpack_from('<i', content, 12)[0]
        if lz4_sz <= 0 or lz4_sz > sz - 16:
            continue
        try:
            raw = lz4_decomp(bytes(content[16 : 16 + lz4_sz]))
        except Exception:
            continue
        if len(raw) >= CHUNK_TILES:
            chunks[(cx, cz)] = raw
    return chunks

# ── тайл ─────────────────────────────────────────────────────────────────────

def get_tile(raw, lx, ly, lz):
    """ZYX layout: tile = raw[z*H*W + y*W + x]"""
    return raw[lz * CH * CW + ly * CW + lx]

# ── конвертация ───────────────────────────────────────────────────────────────

def convert(chunks, y_min=0, y_max=127, surface_only=False):
    all_cx = [k[0] for k in chunks]
    all_cz = [k[1] for k in chunks]
    wx_min = min(all_cx);  wz_min = min(all_cz)
    wx_max = max(all_cx) + CW - 1
    wz_max = max(all_cz) + CD - 1
    W = wx_max - wx_min + 1
    L = wz_max - wz_min + 1

    if surface_only:
        arr = np.zeros(W * L, dtype=np.int8)
        for (cx, cz), raw in chunks.items():
            for lz in range(CD):
                for lx in range(CW):
                    for ly in range(CH - 1, -1, -1):
                        bt = get_tile(raw, lx, ly, lz)
                        if bt != 0:
                            wx = cx + lx - wx_min
                            wz = cz + lz - wz_min
                            arr[wz * W + wx] = kb_to_mc(bt)
                            break
        return arr, W, 1, L, wx_min, 0, wz_min

    y_min = max(0, y_min)
    y_max = min(CH - 1, y_max)
    H = y_max - y_min + 1
    arr = np.zeros(W * H * L, dtype=np.int8)
    for (cx, cz), raw in chunks.items():
        for lz in range(CD):
            for ly in range(y_min, y_max + 1):
                for lx in range(CW):
                    bt = get_tile(raw, lx, ly, lz)
                    mc = kb_to_mc(bt)
                    wx = cx + lx - wx_min
                    wy = ly - y_min
                    wz = cz + lz - wz_min
                    arr[(wy * L + wz) * W + wx] = mc
    return arr, W, H, L, wx_min, y_min, wz_min

# ── запись schematic ──────────────────────────────────────────────────────────

def save_schematic(arr, W, H, L, out_path):
    root = nbtlib.File({
        'Width':        nbtlib.Short(W),
        'Height':       nbtlib.Short(H),
        'Length':       nbtlib.Short(L),
        'Materials':    nbtlib.String('Alpha'),
        'Blocks':       nbtlib.ByteArray(arr.astype(np.int8)),
        'Data':         nbtlib.ByteArray(np.zeros(W * H * L, dtype=np.int8)),
        'Entities':     nbtlib.List[nbtlib.Compound]([]),
        'TileEntities': nbtlib.List[nbtlib.Compound]([]),
    })
    root.save(out_path, gzipped=True)

# ── main ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description='Kiloblocks .dat → Minecraft .schematic')
    p.add_argument('input',             help='Входной .dat файл')
    p.add_argument('-o', '--output',    help='Выходной .schematic')
    p.add_argument('--surface',         action='store_true',
                   help='Только верхний слой (H=1)')
    p.add_argument('--ymin', type=int,  default=0,
                   help='Минимальный Y тайловый (0..127, default=0)')
    p.add_argument('--ymax', type=int,  default=127,
                   help='Максимальный Y тайловый (0..127, default=127)')
    p.add_argument('--list-blocks',     action='store_true',
                   help='Показать статистику блоков в мире')
    args = p.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"Файл не найден: {args.input}")

    print(f"Читаю {args.input}...")
    chunks = load_chunks(args.input)
    if not chunks:
        sys.exit("Не найдено ни одного чанка")
    print(f"  Чанков: {len(chunks)}")

    if args.list_blocks:
        from collections import Counter
        counts = Counter()
        for raw in chunks.values():
            for i in range(CHUNK_TILES):
                bt = raw[i]
                if bt != 0:
                    counts[bt] += 1
        print("\nБлоки в мире (S-Index → название → кол-во):")
        for bt, cnt in counts.most_common():
            print(f"  S={bt:3d}  {kb_name(bt):30s}  {cnt:,}")
        print()

    if args.output:
        out_path = args.output
    else:
        base   = os.path.splitext(args.input)[0]
        suffix = '_surface' if args.surface else ''
        out_path = base + suffix + '.schematic'

    print("Конвертирую...")
    arr, W, H, L, wx_min, wy_min, wz_min = convert(
        chunks,
        y_min=args.ymin,
        y_max=args.ymax,
        surface_only=args.surface,
    )
    non_air = int(np.count_nonzero(arr))
    print(f"  Размер: {W} × {H} × {L}  ({W*H*L:,} ячеек,  {non_air:,} непустых)")
    print(f"  Начало в мире: X={wx_min}  Y={wy_min}  Z={wz_min}")

    print(f"Сохраняю {out_path}...")
    save_schematic(arr, W, H, L, out_path)
    print(f"  Готово! {os.path.getsize(out_path):,} байт")


if __name__ == '__main__':
    main()
