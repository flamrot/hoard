"""Generate Hoard's Lottie animations (startup, loading, scanning, done) as JSON.

    python packaging/make_animations.py      -> hoard/assets/{startup,loading,scanning,done}.json

Everything is built from the logo's geometry (the "H" of treemap blocks, 64-unit grid),
so the animations stay on-brand when the logo changes. No dependencies.
"""
import json
import os

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hoard", "assets")
FR = 60

BLUE, ORANGE, GREEN, YELLOW = "#3d8bf2", "#ef6b3a", "#22b07d", "#f5c44a"
PURPLE, PINK, TEAL, GREY = "#8b6cf0", "#e8508c", "#2bb3c0", "#a9a69d"
TILE = "#f3efe7"

# cubic-bezier easings (x1, y1, x2, y2)
OUT_BACK = (0.34, 1.56, 0.64, 1)
OUT_BACK_SOFT = (0.3, 1.3, 0.6, 1)
OUT = (0.16, 1, 0.3, 1)
IN_OUT = (0.65, 0, 0.35, 1)
LINEAR = (0, 0, 1, 1)


def rgba(hexc, a=1):
    h = hexc.lstrip("#")
    return [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)] + [a]


def static(v):
    return {"a": 0, "k": v}


def anim(keys, dims=1):
    """keys: [(frame, value, easing)]; the easing of a key shapes the move to the next key."""
    out = []
    for i, (t, v, e) in enumerate(keys):
        k = {"t": t, "s": v if isinstance(v, list) else [v]}
        if i < len(keys) - 1:
            e = e or LINEAR
            k["o"] = {"x": [e[0]] * dims, "y": [e[1]] * dims}
            k["i"] = {"x": [e[2]] * dims, "y": [e[3]] * dims}
        out.append(k)
    return {"a": 1, "k": out}


def tr_shape():
    return {"ty": "tr", "p": static([0, 0]), "a": static([0, 0]), "s": static([100, 100]), "r": static(0),
            "o": static(100), "sk": static(0), "sa": static(0), "nm": "Transform"}


def rect(w, h, r, color, off=(0, 0)):
    return {"ty": "gr", "nm": "block", "it": [
        {"ty": "rc", "d": 1, "s": static([w, h]), "p": static(list(off)), "r": static(r), "nm": "rect"},
        {"ty": "fl", "c": static(rgba(color)), "o": static(100), "r": 1, "bm": 0, "nm": "fill"},
        tr_shape()]}


def layer(ind, name, shapes, op, p, a=(0, 0), s=None, o=None, r=None):
    return {"ddd": 0, "ind": ind, "ty": 4, "nm": name, "sr": 1, "ao": 0, "ip": 0, "op": op, "st": 0, "bm": 0,
            "ks": {"o": o or static(100), "r": r or static(0),
                   "p": p if isinstance(p, dict) else static([p[0], p[1], 0]),
                   "a": static([a[0], a[1], 0]), "s": s or static([100, 100, 100])},
            "shapes": shapes}


def doc(name, w, h, op, layers):
    for i, l in enumerate(layers, 1):
        l["ind"] = i
    return {"v": "5.7.4", "fr": FR, "ip": 0, "op": op, "w": w, "h": h, "nm": name, "ddd": 0, "assets": [],
            "layers": layers}


# logo geometry on the 64-unit grid: (x, y, w, h, radius, colour)
H_BLUE = (11, 10, 15, 44, 4, BLUE)
H_ORANGE = (38, 10, 15, 21, 4, ORANGE)
H_GREEN = (38, 34, 15, 20, 4, GREEN)
H_YELLOW = (28.5, 26, 7, 12, 2.5, YELLOW)


def startup():
    """The logo assembles: tile settles in, the tall blue bar grows up, orange drops in,
    green rises, and the yellow crossbar pops to complete the H. 1.2 s, then holds."""
    S, op = 4, 78                                  # 64 units -> 256 px
    def u(v): return v * S

    def block(geom, name, kind, t0):
        x, y, w, h, r, c = (u(g) if i < 5 else g for i, g in enumerate(geom))
        cx, cy = x + w / 2, y + h / 2
        if kind == "grow":                         # grow upwards from the bottom edge
            return layer(0, name, [rect(w, h, r, c, (0, -h / 2))], op, (cx, y + h),
                         s=anim([(t0, [100, 0, 100], OUT_BACK_SOFT), (t0 + 24, [100, 100, 100], None)], 3),
                         o=anim([(t0, 0, OUT), (t0 + 6, 100, None)]))
        if kind in ("drop", "rise"):
            d = -u(10) if kind == "drop" else u(10)
            return layer(0, name, [rect(w, h, r, c)], op,
                         anim([(t0, [cx, cy + d, 0], OUT_BACK_SOFT), (t0 + 22, [cx, cy, 0], None)], 1),
                         o=anim([(t0, 0, OUT), (t0 + 10, 100, None)]))
        return layer(0, name, [rect(w, h, r, c)], op, (cx, cy),                     # pop
                     s=anim([(t0, [0, 0, 100], OUT_BACK), (t0 + 18, [100, 100, 100], None)], 3),
                     o=anim([(t0, 0, OUT), (t0 + 4, 100, None)]))

    tile = layer(0, "tile", [rect(u(60), u(60), u(14), TILE)], op, (u(32), u(32)),
                 s=anim([(0, [86, 86, 100], OUT), (18, [100, 100, 100], None)], 3),
                 o=anim([(0, 0, OUT), (10, 100, None)]))
    layers = [block(H_YELLOW, "yellow", "pop", 36), block(H_GREEN, "green", "rise", 22),
              block(H_ORANGE, "orange", "drop", 14), block(H_BLUE, "blue", "grow", 6), tile]
    return doc("Hoard startup", u(64), u(64), op, layers)


def loading():
    """The four logo blocks breathe in a wave, left to right. Loops every 1.2 s."""
    S, op = 2, 72                                  # 64 units -> 128 px (crop to the blocks)
    ox, oy = 10, 9                                 # crop: blocks span x 11..53, y 10..54
    layers = []
    for i, geom in enumerate((H_BLUE, H_YELLOW, H_ORANGE, H_GREEN)):
        x, y, w, h, r, c = geom
        cx, cy = (x - ox + w / 2) * S, (y - oy + h / 2) * S
        t0 = i * 9
        keys_s = [(0, [100, 100, 100], IN_OUT), (t0, [100, 100, 100], IN_OUT), (t0 + 16, [72, 72, 100], IN_OUT),
                  (t0 + 32, [100, 100, 100], LINEAR), (op, [100, 100, 100], None)]
        keys_o = [(0, 100, IN_OUT), (t0, 100, IN_OUT), (t0 + 16, 45, IN_OUT), (t0 + 32, 100, LINEAR), (op, 100, None)]
        keys_s = [k for j, k in enumerate(keys_s) if j == 0 or k[0] > keys_s[j - 1][0]]
        keys_o = [k for j, k in enumerate(keys_o) if j == 0 or k[0] > keys_o[j - 1][0]]
        layers.append(layer(0, f"b{i}", [rect(w * S, h * S, r * S, c)], op, (cx, cy),
                            s=anim(keys_s, 3), o=anim(keys_o)))
    return doc("Hoard loading", 44 * S, 46 * S, op, layers)


def scanning():
    """A treemap being discovered: blocks pop in one by one until the map is full.
    Meant to be driven by scan progress (frame = progress), or looped when the total is unknown."""
    op = 120
    gap = 4
    blocks = [  # x, y, w, h, colour, in what order they appear
        (10, 10, 128, 110, GREEN), (142, 10, 100, 96, ORANGE), (246, 10, 64, 96, BLUE),
        (10, 124, 62, 66, BLUE), (142, 110, 64, 80, PURPLE), (210, 110, 100, 40, YELLOW),
        (76, 124, 62, 66, GREY), (210, 154, 48, 36, TEAL), (262, 154, 48, 36, PINK),
    ]
    layers = []
    span = 96 / len(blocks)
    for i, (x, y, w, h, c) in enumerate(blocks):
        t0 = round(4 + i * span)
        layers.append(layer(0, f"t{i}", [rect(w - gap / 2, h - gap / 2, 6, c)], op, (x + w / 2, y + h / 2),
                            s=anim([(t0, [0, 0, 100], OUT_BACK), (t0 + 16, [100, 100, 100], None)], 3),
                            o=anim([(t0, 0, OUT), (t0 + 6, 100, None)])))
    # outline of the "disk" the map fills
    frame = {"ty": "gr", "nm": "frame", "it": [
        {"ty": "rc", "d": 1, "s": static([308, 188]), "p": static([0, 0]), "r": static(10), "nm": "rect"},
        {"ty": "st", "c": static(rgba(GREY, 1)), "o": static(55), "w": static(2), "lc": 2, "lj": 2, "ml": 4,
         "bm": 0, "nm": "stroke", "d": [{"n": "d", "nm": "dash", "v": static(6)}, {"n": "g", "nm": "gap", "v": static(6)},
                                       {"n": "o", "nm": "offset", "v": anim([(0, 0, LINEAR), (op, -48, None)])}]},
        tr_shape()]}
    layers.reverse()
    layers.append(layer(0, "frame", [frame], op, (160, 100)))
    return doc("Hoard scanning", 320, 200, op, layers)


def done():
    """Space freed: a green disc pops, a check draws itself, and four logo-coloured blocks burst out."""
    op, c = 54, 64                                 # 128 px canvas, centre 64
    disc = {"ty": "gr", "nm": "disc", "it": [
        {"ty": "el", "d": 1, "s": static([76, 76]), "p": static([0, 0]), "nm": "ellipse"},
        {"ty": "fl", "c": static(rgba(GREEN)), "o": static(100), "r": 1, "bm": 0, "nm": "fill"}, tr_shape()]}
    check = {"ty": "gr", "nm": "check", "it": [
        {"ty": "sh", "d": 1, "nm": "path", "ks": static({"c": False, "v": [[-15, 1], [-4, 12], [16, -10]],
                                                          "i": [[0, 0], [0, 0], [0, 0]], "o": [[0, 0], [0, 0], [0, 0]]})},
        {"ty": "tm", "s": static(0), "e": anim([(12, 0, OUT), (30, 100, None)]), "o": static(0), "m": 1, "nm": "trim"},
        {"ty": "st", "c": static(rgba("#ffffff")), "o": static(100), "w": static(8), "lc": 2, "lj": 2, "ml": 4,
         "bm": 0, "nm": "stroke"},
        tr_shape()]}
    layers = [layer(0, "check", [check], op, (c, c)),
              layer(0, "disc", [disc], op, (c, c),
                    s=anim([(0, [0, 0, 100], OUT_BACK), (18, [100, 100, 100], None)], 3))]
    for i, (dx, dy, col) in enumerate(((-1, -1, BLUE), (1, -1, ORANGE), (1, 1, GREEN), (-1, 1, YELLOW))):
        layers.append(layer(0, f"burst{i}", [rect(10, 10, 3, col)], op,
                            anim([(6, [c + dx * 30, c + dy * 30, 0], OUT), (34, [c + dx * 54, c + dy * 54, 0], None)], 1),
                            s=anim([(6, [40, 40, 100], OUT), (20, [100, 100, 100], IN_OUT), (34, [0, 0, 100], None)], 3),
                            o=anim([(6, 0, OUT), (10, 100, IN_OUT), (30, 100, IN_OUT), (34, 0, None)]),
                            r=anim([(6, 0, OUT), (34, 90 * dx * dy, None)])))
    return doc("Hoard done", 128, 128, op, layers)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, fn in (("startup", startup), ("loading", loading), ("scanning", scanning), ("done", done)):
        with open(os.path.join(OUT_DIR, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(fn(), f, separators=(",", ":"))
        print("wrote", name + ".json")


if __name__ == "__main__":
    main()
