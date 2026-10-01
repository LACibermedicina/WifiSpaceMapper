"""
Superficie a partir da nuvem de pontos -- caminho de reserva (somente NumPy/SciPy).

Usado quando o Open3D nao esta disponivel (o caminho principal continua sendo o
Poisson/Alpha Shapes do pacote `csi_mapper.reconstruction`). Aqui:
  nuvem -> grade de ocupacao (voxels) -> faces expostas (malha fechada) -> metricas
Tambem calcula o volume mapeado, usado no historico de areas.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np

try:  # scipy e dependencia do pipeline de DSP, mas mantemos o modo degradado
    from scipy import ndimage as ndi
    SCIPY_OK = True
except Exception:  # pragma: no cover
    ndi = None
    SCIPY_OK = False


def voxel_grid(points: np.ndarray, voxel: float, dilate: int = 0):
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(pts) == 0:
        return None
    lo = pts.min(axis=0) - 2.0 * voxel
    hi = pts.max(axis=0) + 2.0 * voxel
    dims = np.maximum(np.ceil((hi - lo) / voxel).astype(int), 1)
    grid = np.zeros(tuple(dims), dtype=bool)
    ij = np.clip(np.floor((pts - lo) / voxel).astype(int), 0, dims - 1)
    grid[ij[:, 0], ij[:, 1], ij[:, 2]] = True
    if dilate and SCIPY_OK:
        grid = ndi.binary_dilation(grid, iterations=int(dilate))
    return lo, voxel, grid


def grid_to_mesh(lo, voxel: float, grid: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Faces expostas da grade de ocupacao -> (vertices (V,3), triangles (F,3))."""
    verts: Dict[Tuple[float, float, float], int] = {}
    vlist = []
    tris = []

    def vid(p):
        key = (round(float(p[0]), 5), round(float(p[1]), 5), round(float(p[2]), 5))
        i = verts.get(key)
        if i is None:
            i = len(vlist)
            verts[key] = i
            vlist.append(key)
        return i

    occ = np.argwhere(grid)
    dims = grid.shape
    for (i, j, k) in occ:
        base = np.array([i, j, k])
        for axis in range(3):
            for s in (-1, 1):
                nb = base.copy()
                nb[axis] += s
                if 0 <= nb[axis] < dims[axis] and grid[nb[0], nb[1], nb[2]]:
                    continue
                o = lo + base * voxel
                u = np.zeros(3)
                v = np.zeros(3)
                u[(axis + 1) % 3] = voxel
                v[(axis + 2) % 3] = voxel
                face_o = o.copy()
                if s > 0:
                    face_o[axis] += voxel
                c = [face_o, face_o + u, face_o + u + v, face_o + v]
                idx = [vid(p) for p in c]
                tris.append([idx[0], idx[1], idx[2]])
                tris.append([idx[0], idx[2], idx[3]])

    if not vlist:
        return np.zeros((0, 3), np.float32), np.zeros((0, 3), np.int32)
    return (np.asarray(vlist, dtype=np.float32),
            np.asarray(tris, dtype=np.int32))


def voxel_surface(points: np.ndarray, voxel: float = 0.12,
                  dilate: int = 0) -> dict:
    """Reconstrucao por voxels -> dict com malha, volume e estatisticas."""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    out = {"vertices": np.zeros((0, 3), np.float32),
           "triangles": np.zeros((0, 3), np.int32),
           "voxel": float(voxel),
           "occupied": 0,
           "volume_m3": 0.0,
           "bbox": [0.0, 0.0, 0.0],
           "method": "voxels"}
    if len(pts) < 4:
        return out
    g = voxel_grid(pts, voxel, dilate=dilate)
    if g is None:
        return out
    lo, vx, grid = g
    v, f = grid_to_mesh(lo, vx, grid)
    n_occ = int(grid.sum())
    out["vertices"] = v
    out["triangles"] = f
    out["occupied"] = n_occ
    out["volume_m3"] = float(n_occ) * float(vx) ** 3
    ext = grid.shape
    out["bbox"] = [float(ext[0] * vx), float(ext[1] * vx), float(ext[2] * vx)]
    out["bounds"] = {"lo": [float(x) for x in lo],
                     "hi": [float(lo[i] + ext[i] * vx) for i in range(3)]}
    out["method"] = "voxels" if not SCIPY_OK else "voxels(scipy)"
    return out


def coverage_stats(points: np.ndarray, room=None) -> Dict[str, float]:
    """Metricas de erro/cobertura contra a geometria real (quando conhecida)."""
    metrics: Dict[str, float] = {}
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(pts) == 0:
        return {"err_mean_m": 0.0, "err_median_m": 0.0, "coverage_25cm": 0.0,
                "coverage_50cm": 0.0}
    if room is not None:
        try:
            from csi_mapper.room import plane_distance
            d = np.asarray(plane_distance(pts, room), dtype=np.float64)
            metrics["err_mean_m"] = float(np.mean(d))
            metrics["err_median_m"] = float(np.median(d))
            metrics["err_p90_m"] = float(np.percentile(d, 90))
            metrics["coverage_25cm"] = float(np.mean(d < 0.25))
            metrics["coverage_50cm"] = float(np.mean(d < 0.50))
        except Exception:
            pass
    r = np.linalg.norm(pts, axis=1)
    metrics["radius_mean_m"] = float(np.mean(r))
    metrics["radius_max_m"] = float(np.max(r))
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    metrics["bbox_x"] = float(hi[0] - lo[0])
    metrics["bbox_y"] = float(hi[1] - lo[1])
    metrics["bbox_z"] = float(hi[2] - lo[2])
    return metrics
