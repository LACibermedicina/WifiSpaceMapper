"""
Utilitários de cor: mapa de distância (azul perto -> vermelho longe).
"""
from __future__ import annotations

import numpy as np

# paradas do gradiente: [t, R, G, B, A]  azul -> ciano -> verde -> amarelo -> vermelho
_STOPS = np.array([
    [0.00, 0.03, 0.14, 0.88, 1.00],
    [0.25, 0.00, 0.72, 0.96, 1.00],
    [0.50, 0.10, 0.86, 0.36, 1.00],
    [0.75, 0.99, 0.86, 0.10, 1.00],
    [1.00, 0.96, 0.11, 0.16, 1.00],
], dtype=np.float64)


def normalize(values: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    """Normaliza para [0,1] com proteção para intervalo degenerado."""
    v = np.asarray(values, dtype=np.float64)
    span = float(vmax - vmin)
    if abs(span) < 1e-12:
        return np.zeros_like(v)
    return np.clip((v - vmin) / span, 0.0, 1.0)


def distance_colormap(values: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    """(N,) distâncias -> (N,4) RGBA float32."""
    t = normalize(values, vmin, vmax)
    out = np.empty((len(t), 4), dtype=np.float32)
    for ch in range(4):
        out[:, ch] = np.interp(t, _STOPS[:, 0], _STOPS[:, ch + 1])
    return out


def colormap_lut(n: int = 256) -> np.ndarray:
    """LUT (n,4) para uso como colormap de imagens (espectro AoA)."""
    t = np.linspace(0.0, 1.0, n)
    out = np.empty((n, 4), dtype=np.float32)
    for ch in range(4):
        out[:, ch] = np.interp(t, _STOPS[:, 0], _STOPS[:, ch + 1])
    return out


# cores fixas da interface 3D
COL_GRID = (0.32, 0.36, 0.42, 0.55)
COL_ROOM = (0.45, 0.70, 1.00, 0.70)
COL_TRAJ = (1.00, 0.80, 0.20, 0.85)
COL_AXIS = {
    "x": (0.95, 0.20, 0.20, 1.0),
    "y": (0.25, 0.90, 0.30, 1.0),
    "z": (0.30, 0.55, 1.00, 1.0),
}
COL_ROUTER_BODY = (0.06, 0.07, 0.10, 1.0)
COL_ROUTER_NEON = (0.00, 0.92, 1.00, 1.0)
COL_GT_ROOM = (1.00, 1.00, 1.00, 0.16)
