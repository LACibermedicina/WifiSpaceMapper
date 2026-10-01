"""
Ponte entre o pacote original `csi_mapper` e o portal web.

Reaproveita integralmente o pipeline ja existente:
  * MockCSISource  -> sintese do canal MIMO-OFDM (tracado de raios, metodo da imagem)
  * MappingPipeline-> calibracao + MUSIC 2D (AoA) + MUSIC 1D (ToF) + geometria
  * reconstruct()  -> voxel / SOR / DBSCAN / Poisson / Alpha Shapes (Open3D)

Se o Open3D nao estiver instalado, a malha e gerada pelo modulo `surface`
(voxels) e o resultado continua valido. Se o pipeline de DSP nao puder ser
importado, entra o gerador de reserva (mesmo tracado de raios, sem MUSIC).
"""
from __future__ import annotations

import math
import os
import sys
import time
from typing import Dict, List, Optional

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import surface as surf

CSI_OK = False
O3D_OK = False
_IMPORT_ERROR = ""
try:
    from csi_mapper.config import default_config
    from csi_mapper.room import Room, plane_distance  # noqa: F401
    from csi_mapper.csi_source import MockCSISource
    from csi_mapper.dsp import MappingPipeline
    from csi_mapper.reconstruction import O3D_OK, reconstruct
    CSI_OK = True
except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    O3D_OK = False

PRESETS = {
    # pontos por volta / alturas / passo MUSIC grosso
    "rapida": {"points_per_lap": 34, "heights": (0.55, -0.75), "packets": 4,
               "az_step": 5.0, "el_step": 6.0, "voxel": 0.14},
    "padrao": {"points_per_lap": 48, "heights": (0.55, -0.15, -0.85), "packets": 6,
               "az_step": 3.5, "el_step": 4.0, "voxel": 0.12},
    "precisa": {"points_per_lap": 60, "heights": (0.55, -0.15, -0.85), "packets": 8,
                "az_step": 2.5, "el_step": 3.0, "voxel": 0.10},
}


def capabilities() -> dict:
    return {"csi_pipeline": CSI_OK, "open3d": bool(O3D_OK),
            "import_error": _IMPORT_ERROR,
            "numpy": np.__version__,
            "mode": "csi_mapper" if CSI_OK else "fallback"}


# --------------------------------------------------------------------------
# Reserva: tracado de raios simplificado (mesma fisica do room.py)
# --------------------------------------------------------------------------
class _MiniRoom:
    """Copomodo retangular + tracado de primeira ordem (metodo da imagem)."""

    def __init__(self, size, router_height, obstacles=()):
        self.sx, self.sy, self.sz = size
        self.rh = router_height
        self.xlim = (-self.sx / 2, self.sx / 2)
        self.ylim = (-self.sy / 2, self.sy / 2)
        self.zlim = (-self.rh, self.sz - self.rh)
        self.obstacles = list(obstacles)
        self.planes = self._planes()

    def _planes(self):
        (x0, x1), (y0, y1), (z0, z1) = self.xlim, self.ylim, self.zlim
        ex, ey, ez = np.eye(3)
        P = []
        P.append((np.array([x0, y0, z0]), ey, ez, ex, y1 - y0, z1 - z0, 0.62))
        P.append((np.array([x1, y0, z0]), ey, ez, -ex, y1 - y0, z1 - z0, 0.62))
        P.append((np.array([x0, y0, z0]), ex, ez, ey, x1 - x0, z1 - z0, 0.62))
        P.append((np.array([x0, y1, z0]), ex, ez, -ey, x1 - x0, z1 - z0, 0.62))
        P.append((np.array([x0, y0, z0]), ex, ey, ez, x1 - x0, y1 - y0, 0.40))
        P.append((np.array([x0, y0, z1]), ex, ey, -ez, x1 - x0, y1 - y0, 0.34))
        return P

    def _contains(self, p) -> bool:
        return (self.xlim[0] <= p[0] <= self.xlim[1]
                and self.ylim[0] <= p[1] <= self.ylim[1]
                and self.zlim[0] <= p[2] <= self.zlim[1])

    def _blocked(self, a, b, n=12) -> bool:
        for t in np.linspace(0.05, 0.95, n):
            p = a + t * (b - a)
            if not self._contains(p):
                return True
            for (c, h) in self.obstacles:
                if np.all(np.abs(p - np.asarray(c)) <= np.asarray(h)):
                    return True
        return False

    def trace(self, rx):
        out = []
        d_los = float(np.linalg.norm(rx))
        if d_los > 1e-6 and not self._blocked(np.zeros(3), rx):
            out.append((rx.copy(), d_los, 1.0 / (d_los * d_los)))
        tx = np.zeros(3)
        for (o, u, v, nrm, um, vm, refl) in self.planes:
            # imagem especular do TX (na origem) em relacao ao plano
            img = tx - 2.0 * float((tx - o) @ nrm) * nrm
            d = img - rx
            den = float(d @ nrm)
            if abs(den) < 1e-12:
                continue
            t = float((o - rx) @ nrm) / den
            if not (-1e-6 <= t <= 1.0 + 1e-6):
                continue
            w = rx + t * d
            rel = w - o
            a, b = float(rel @ u), float(rel @ v)
            if not (0 <= a <= um and 0 <= b <= vm):
                continue
            d1 = float(np.linalg.norm(w))
            d2 = float(np.linalg.norm(rx - w))
            if d1 < 0.05 or d2 < 0.05:
                continue
            if float((tx - w) @ nrm) <= 0 or float((rx - w) @ nrm) <= 0:
                continue
            if self._blocked(np.zeros(3), w) or self._blocked(w, rx):
                continue
            out.append((w.copy(), d1 + d2, refl / (d1 * d2)))
        out.sort(key=lambda r: r[1])
        return out

    def trajectory(self, points_per_lap, heights, a=None, b=None):
        a = a if a else max(0.35, self.sx / 2.0 - 0.55)
        b = b if b else max(0.35, self.sy / 2.0 - 0.65)
        pos = []
        for z in heights:
            for i in range(points_per_lap):
                t = 2.0 * math.pi * i / points_per_lap
                pos.append([a * math.cos(t), b * math.sin(t), z])
        return np.asarray(pos, dtype=np.float64)


def _fallback_cloud(params: dict):
    """Nuvem de pontos direta do tracado de raios (sem MUSIC)."""
    preset = PRESETS.get(params.get("preset", "rapida"), PRESETS["rapida"])
    room = _MiniRoom((params["room_x"], params["room_y"], params["room_z"]),
                     params["router_height"], params.get("obstacles") or [])
    traj = room.trajectory(preset["points_per_lap"], preset["heights"])
    pts, strengths = [], []
    for rx in traj:
        for (w, L, ampl) in room.trace(rx)[:4]:
            if L < 0.65:
                continue
            pts.append(w)
            strengths.append(ampl / 1e-3)
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
    strengths = np.asarray(strengths, dtype=np.float64).reshape(-1)
    if len(strengths):
        strengths = strengths / max(float(strengths.max()), 1e-12)
    return pts, strengths, traj


# --------------------------------------------------------------------------
# Execucao principal
# --------------------------------------------------------------------------
def run_capture(params: dict) -> dict:
    t0 = time.time()
    preset_name = str(params.get("preset", "rapida"))
    preset = PRESETS.get(preset_name, PRESETS["rapida"])
    p = {
        "room_x": float(params.get("room_x", 4.0)),
        "room_y": float(params.get("room_y", 5.0)),
        "room_z": float(params.get("room_z", 2.8)),
        "router_height": float(params.get("router_height", 1.4)),
        "voxel": float(params.get("voxel", preset["voxel"])),
        "seed": int(params.get("seed", 7)),
        "preset": preset_name,
        "obstacles": params.get("obstacles") or [],
    }
    room_obj = None
    mesh = None
    method = ""
    metrics: Dict[str, float] = {}
    calib: Dict[str, object] = {}
    traj = np.zeros((0, 3))
    forces = None
    soft = None

    traj = np.asarray(traj, dtype=np.float64).reshape(-1, 3)
    if CSI_OK:
        cfg = default_config()
        cfg.room.size = (p["room_x"], p["room_y"], p["room_z"])
        cfg.room.router_height = p["router_height"]
        if p["obstacles"]:
            from csi_mapper.config import Box
            boxes = []
            for o in p["obstacles"][:12]:
                try:
                    boxes.append(Box(center=tuple(float(v) for v in o["center"]),
                                     half=tuple(float(v) for v in o["half"]),
                                     reflectivity=float(o.get("reflectivity", 0.42)),
                                     label=str(o.get("label", "obstaculo"))))
                except Exception:
                    continue
            cfg.room.obstacles = boxes
        cfg.source = "mock"
        cfg.scan.points_per_lap = int(preset["points_per_lap"])
        cfg.scan.lap_heights = tuple(preset["heights"])
        cfg.scan.packets_per_pos = int(preset["packets"])
        cfg.dsp.az_step_coarse = float(preset["az_step"])
        cfg.dsp.el_step_coarse = float(preset["el_step"])
        cfg.recon.voxel = p["voxel"]

        source = MockCSISource(cfg, seed=p["seed"])
        room_obj = Room(cfg.room)
        pipe = MappingPipeline(cfg, room_obj, source, on_progress=None,
                               pace=False, cancel=lambda: False)
        res = pipe.run()
        pts = np.asarray(getattr(res, "points", np.zeros((0, 3))), dtype=np.float64)
        strengths = np.asarray(getattr(res, "strengths", np.zeros(len(pts))),
                               dtype=np.float64).reshape(-1)
        if len(strengths) != len(pts):
            strengths = np.ones(len(pts))
        try:
            rec = reconstruct(pts, strengths, cfg, room_obj, use_poisson=True)
            method = rec.method
            metrics.update({k: float(v) for k, v in (rec.metrics or {}).items()})
            for key in ("vertices", "triangles", "points_dbscan", "points_raw",
                        "dbscan_clusters", "err_cloud_mean_m", "err_surface_mean_m",
                        "cover_cloud_25cm", "cover_surface_25cm", "bbox_x", "bbox_y",
                        "bbox_z", "err_cloud_median_m"):
                if key in (rec.metrics or {}):
                    metrics[key] = float(rec.metrics[key])
            if getattr(rec, "has_mesh", False):
                mesh = {"vertices": np.asarray(rec.vertices, dtype=np.float32),
                        "triangles": np.asarray(rec.triangles, dtype=np.int32)}
        except Exception as exc:
            method = f"reconstrucao-falhou({type(exc).__name__})"
        stats = getattr(res, "stats", {}) or {}
        for k, v in stats.items():
            try:
                metrics[f"stats_{k}"] = float(v)
            except (TypeError, ValueError):
                continue
        cal = getattr(res, "calib", None)
        if cal is not None:
            calib = {"los_len_m": float(getattr(cal, "los_len", 0.0)),
                     "los_az_deg": float(getattr(cal, "los_az_deg", 0.0)),
                     "los_el_deg": float(getattr(cal, "los_el_deg", 0.0)),
                     "snr_db": float(getattr(cal, "snr_db", 0.0)),
                     "packets_used": int(getattr(cal, "packets_used", 0))}
        tj = getattr(res, "traj", None)
        if tj is not None and len(np.asarray(tj)):
            traj = np.asarray(tj, dtype=np.float64).reshape(-1, 3)
        forces = {"mode": "csi_mapper", "dsp": True}
    else:
        pts, strengths, traj = _fallback_cloud(p)
        method = "tracado-direto (sem csi_mapper)"
        soft = _IMPORT_ERROR
        forces = {"mode": "fallback", "dsp": False, "reason": _IMPORT_ERROR}

    if not len(traj) and len(pts):
        traj = np.zeros((0, 3))

    vox = surf.voxel_surface(pts, p["voxel"],
                             dilate=0 if (mesh is not None) else 0)
    if mesh is None and len(vox["vertices"]):
        mesh = {"vertices": vox["vertices"], "triangles": vox["triangles"]}
        method = f"{method} + {vox['method']}"

    metrics.update(surf.coverage_stats(pts, room_obj))
    metrics["n_points"] = float(len(pts))
    metrics["n_triangles"] = float(len(mesh["triangles"]) if mesh else 0)
    metrics["volume_voxel_m3"] = float(vox["volume_m3"])
    metrics["voxel_m"] = float(p["voxel"])
    metrics["duration_s"] = float(time.time() - t0)
    metrics["room_x"] = p["room_x"]
    metrics["room_y"] = p["room_y"]
    metrics["room_z"] = p["room_z"]

    return {"points": np.asarray(pts, dtype=np.float32),
            "strengths": np.asarray(strengths, dtype=np.float32),
            "mesh": mesh,
            "voxels": {"voxel": p["voxel"], "occupied": vox["occupied"],
                       "volume_m3": vox["volume_m3"]},
            "metrics": metrics, "calib": calib,
            "traj": np.asarray(traj, dtype=float).reshape(-1, 3).round(4).tolist(),
            "method": method, "params": p, "forces": forces,
            "soft_error": soft,
            "capabilities": capabilities()}
