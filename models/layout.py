"""Layout data for instancing / ECS -> layout.json (not a mesh)."""


def build_json(assets):
    """Plain data an ECS / instancing renderer consumes. glTF axes, metres."""
    bays, pitch, rows, row_pitch = 8, 1.3, 4, 2.5
    xs = [(i - (bays - 1) / 2) * pitch for i in range(bays)]
    racks, totes = [], []
    levels_m = [0.2, 0.8, 1.4, 2.0, 2.6]
    for r in range(rows):
        for i, x in enumerate(xs):
            rid = f"R{r}-{i}"
            racks.append({"id": rid, "position": [round(x, 3), 0, round(r * row_pitch, 3)], "rotationY": 0})
            for lv, y in enumerate(levels_m):          # two totes per level, deterministic fill pattern
                for slot, dx in enumerate((-0.3, 0.3)):
                    if (r + i + lv + slot) % 3 != 0:
                        totes.append({"id": f"T-{rid}-{lv}-{slot}", "rack": rid, "level": lv,
                                      "position": [round(x + dx, 3), y + 0.102, round(r * row_pitch, 3)]})
    x0, x1 = xs[0] - 1.2, xs[-1] + 1.2
    robots = []
    for k in range(3):
        za = k * row_pitch + row_pitch / 2
        zc = -1.0 - 0.4 * k
        robots.append({"id": f"AMR-{k + 1}", "start": [round(x0, 3), 0, round(za, 3)],
                       "loop": [[round(x0, 3), round(za, 3)], [round(x1, 3), round(za, 3)],
                                [round(x1, 3), round(zc, 3)], [round(x0, 3), round(zc, 3)]],
                       "speedMps": 1.2})
    return {
        "meta": {"units": "metres", "axes": "glTF (Y up, Z toward viewer)", "origin": "asset floor-centre",
                 "bounds": {"x": [round(x0 - 2.5, 2), round(x1 + 3, 2)], "z": [-3.0, round(rows * row_pitch, 2)]}},
        "assets": {k: f"{k}.glb" for k in assets},
        "joints": {"robot": ["wheel_l_spin", "wheel_r_spin", "lift_slide"],
                   "station": ["tower_red", "tower_amber", "tower_green"]},
        "instances": {
            "rack": racks,
            "tote": totes,
            "conveyor": [{"id": "CV-1", "position": [round(x1 + 1.6, 3), 0, 3.75], "rotationY": 90, "beltSpeedMps": 0.4}],
            "station": [{"id": "ST-1", "position": [round(x0 - 1.8, 3), 0, -1.4], "rotationY": 90}],
        },
        "robots": robots,
        "telemetry_example": {"id": "AMR-1", "t": 1731100000.0, "x": -6.5, "z": 1.25, "heading": 0.0,
                              "battery": 0.82, "lift": 0.0, "state": "moving"},
    }
