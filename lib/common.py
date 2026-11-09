"""Shared helpers for every model script."""
import sand3d as s3d

VIOLET, CYAN, PINK, AMBER, RED, GREEN = "#8b6ff5", "#22d3ee", "#e879f9", "#f59e0b", "#ef4444", "#22c55e"


def mat(ob, kind, color=None, **kw):
    s3d.material(ob, kind, color=color, **kw)
    return ob
