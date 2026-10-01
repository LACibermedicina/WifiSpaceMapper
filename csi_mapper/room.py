"""
Modelo geométrico do cômodo + traçado de raios por método da imagem
(specular reflection) e utilitários de trajetória/verdade-terreno.

Este módulo é a "física" do simulador: a partir dele o gerador de CSI
sintetiza o canal MIMO-OFDM real (ToF + AoA) — o pipeline de DSP depois
recupera esses parâmetros de forma cega, como faria com hardware real.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from .config import C_LIGHT, Box, RoomConfig


# --------------------------------------------------------------------------
# Planos refletores
# --------------------------------------------------------------------------
@dataclass
class Plane:
    """Face refletora finita: origem + eixos no plano + extensões + normal."""
    origin: np.ndarray
    u_axis: np.ndarray
    v_axis: np.ndarray
    normal: np.ndarray
    umax: float
    vmax: float
    umin: float = 0.0
    vmin: float = 0.0
    reflectivity: float = 0.5
    label: str = "superfície"

    def contains(self, p: np.ndarray, tol: float = 1e-6) -> bool:
        d = p - self.origin
        a = float(d @ self.u_axis)
        b = float(d @ self.v_axis)
        c = float(d @ self.normal)
        return (self.umin - tol <= a <= self.umax + tol
                and self.vmin - tol <= b <= self.vmax + tol
                and abs(c) <= 1e-4)

    def mirror(self, p: np.ndarray) -> np.ndarray:
        """Imagem especular de p em relação ao plano."""
        return p - 2.0 * float((p - self.origin) @ self.normal) * self.normal

    def hit(self, a: np.ndarray, b: np.ndarray) -> Optional[np.ndarray]:
        """Interseção do segmento a->b com o plano (None se paralelo)."""
        d = b - a
        den = float(d @ self.normal)
        if abs(den) < 1e-12:
            return None
        t = float((self.origin - a) @ self.normal) / den
        if not (-1e-6 <= t <= 1.0 + 1e-6):
            return None
        return a + t * d


# --------------------------------------------------------------------------
# Cômodo
# --------------------------------------------------------------------------
class Room:
    """Cômodo retangular com obstáculos; TX (roteador) na origem."""

    def __init__(self, cfg: RoomConfig):
        self.cfg = cfg
        self.tx = np.zeros(3, dtype=np.float64)
        self.planes: List[Plane] = []
        self._build_planes()
        # amostras densas da superfície real (verdade-terreno / métricas)
        self._gt_cache: Optional[np.ndarray] = None

    # ------------------------------------------------------------------
    def _build_planes(self) -> None:
        cfg = self.cfg
        (x0, x1), (y0, y1), (z0, z1) = cfg.xlim, cfg.ylim, cfg.zlim
        ex = np.array([1.0, 0.0, 0.0])
        ey = np.array([0.0, 1.0, 0.0])
        ez = np.array([0.0, 0.0, 1.0])

        P = self.planes.append
        # paredes: normais apontando para dentro
        P(Plane(np.array([x0, y0, z0]), ey, ez, ex, y1 - y0, z1 - z0,
                reflectivity=cfg.wall_reflectivity, label="parede oeste (-X)"))
        P(Plane(np.array([x1, y0, z0]), ey, ez, -ex, y1 - y0, z1 - z0,
                reflectivity=cfg.wall_reflectivity, label="parede leste (+X)"))
        P(Plane(np.array([x0, y0, z0]), ex, ez, ey, x1 - x0, z1 - z0,
                reflectivity=cfg.wall_reflectivity, label="parede sul (-Y)"))
        P(Plane(np.array([x0, y1, z0]), ex, ez, -ey, x1 - x0, z1 - z0,
                reflectivity=cfg.wall_reflectivity, label="parede norte (+Y)"))
        # piso e teto
        P(Plane(np.array([x0, y0, z0]), ex, ey, ez, x1 - x0, y1 - y0,
                reflectivity=cfg.floor_reflectivity, label="piso"))
        P(Plane(np.array([x0, y0, z1]), ex, ey, -ez, x1 - x0, y1 - y0,
                reflectivity=cfg.ceiling_reflectivity, label="teto"))

        # obstáculos (6 faces cada, normais para fora)
        for b in cfg.obstacles:
            lo = np.array(b.lo)
            hi = np.array(b.hi)
            sx, sy, sz = hi - lo
            faces = [
                (np.array([lo[0], lo[1], lo[2]]), ey, ez, -ex, sy, sz),
                (np.array([hi[0], lo[1], lo[2]]), ey, ez, +ex, sy, sz),
                (np.array([lo[0], lo[1], lo[2]]), ex, ez, -ey, sx, sz),
                (np.array([lo[0], hi[1], lo[2]]), ex, ez, +ey, sx, sz),
                (np.array([lo[0], lo[1], lo[2]]), ex, ey, -ez, sx, sy),
                (np.array([lo[0], lo[1], hi[2]]), ex, ey, +ez, sx, sy),
            ]
            for o, u, v, n, um, vm in faces:
                P(Plane(o, u, v, n, um, vm, reflectivity=b.reflectivity,
                        label=f"{b.label}"))

    # ------------------------------------------------------------------
    # Oclusão / visibilidade
    # ------------------------------------------------------------------
    def blocked(self, a: np.ndarray, b: np.ndarray, ignore_label: str = "",
                n: int = 14) -> bool:
        """True se o segmento a->b sai do cômodo ou cruza um obstáculo."""
        for t in np.linspace(0.06, 0.94, n):
            p = a + t * (b - a)
            if not self.cfg.contains(p):
                return True
            if self.cfg.inside_obstacle(p, tol=-0.02):
                # ignora o próprio obstáculo da face (reflexão na superfície)
                if ignore_label and any(
                        ignore_label.startswith(ob.label)
                        for ob in self.cfg.obstacles):
                    continue
                return True
        return False

    # ------------------------------------------------------------------
    # Traçado de raios (primeira ordem, método da imagem)
    # ------------------------------------------------------------------
    def trace_first_order(self, rx: np.ndarray
                          ) -> List[dict]:
        """
        Percursos de reflexão única TX -> parede -> RX (e o LoS).

        Retorna lista de dicts:
            point     : ponto de reflexão (m)
            length    : caminho total TX->W->RX (m)
            amplitude : refletividade / (d1*d2)  (modelo tipo Friis simplificado)
            label     : superfície
            los       : True para o percurso direto
        """
        out: List[dict] = []
        d_los = float(np.linalg.norm(rx - self.tx))
        rays: List[dict] = []
        if d_los > 1e-6 and not self.blocked(self.tx, rx):
            rays.append(dict(point=rx.copy(), length=d_los,
                             amplitude=1.0 / (d_los * d_los),
                             label="LoS", los=True))

        for pl in self.planes:
            img = pl.mirror(self.tx)
            w = pl.hit(rx, img)
            if w is None or not pl.contains(w):
                continue
            d1 = float(np.linalg.norm(w - self.tx))
            d2 = float(np.linalg.norm(rx - w))
            if d1 < 0.05 or d2 < 0.05:
                continue
            # ponto de reflexão deve estar no lado correto da face
            if float((self.tx - w) @ pl.normal) <= 0 or float((rx - w) @ pl.normal) <= 0:
                continue
            if self.blocked(self.tx, w, ignore_label=pl.label):
                continue
            if self.blocked(w, rx, ignore_label=pl.label):
                continue
            rays.append(dict(point=w, length=d1 + d2,
                             amplitude=pl.reflectivity / (d1 * d2),
                             label=pl.label, los=False))
        rays.sort(key=lambda r: r["length"])
        return rays

    # ------------------------------------------------------------------
    # Trajetória de varredura
    # ------------------------------------------------------------------
    def trajectory(self, scan_cfg) -> Tuple[np.ndarray, np.ndarray]:
        """
        Trajetória elíptica multinível do receptor.

        Retorna
        -------
        positions : (N,3) posições do receptor no frame do roteador
        rotations : (N,3,3) matrizes de rotação local->mundo
                    (colunas: frente, esquerda, cima)
        """
        n_lap = scan_cfg.points_per_lap
        a, b = scan_cfg.ellipse_a, scan_cfg.ellipse_b
        pos, rot = [], []
        for z in scan_cfg.lap_heights:
            for i in range(n_lap):
                t = 2.0 * np.pi * i / n_lap
                p = np.array([a * np.cos(t), b * np.sin(t), z], dtype=np.float64)
                # tangente da elipse -> direção "frente" do dispositivo
                fwd = np.array([-a * np.sin(t), b * np.cos(t), 0.0])
                n = np.linalg.norm(fwd)
                if n < 1e-9:
                    fwd = np.array([1.0, 0.0, 0.0])
                else:
                    fwd = fwd / n
                up = np.array([0.0, 0.0, 1.0])
                left = np.cross(up, fwd)
                left /= max(np.linalg.norm(left), 1e-9)
                R = np.column_stack([fwd, left, up])
                pos.append(p)
                rot.append(R)
        return np.asarray(pos), np.asarray(rot)

    def calibration_pose(self, scan_cfg) -> Tuple[np.ndarray, np.ndarray]:
        """Pose estática usada na etapa de calibração (com LoS limpo)."""
        p = np.asarray(scan_cfg.calib_pos, dtype=np.float64)
        ang = np.deg2rad(scan_cfg.calib_heading_deg)
        fwd = np.array([np.cos(ang), np.sin(ang), 0.0])
        up = np.array([0.0, 0.0, 1.0])
        left = np.cross(up, fwd)
        return p, np.column_stack([fwd, left, up])

    # ------------------------------------------------------------------
    # Verdade-terreno (métricas de acurácia)
    # ------------------------------------------------------------------
    def surface_samples(self, step: float = 0.05) -> np.ndarray:
        """Nuvem densa sobre TODAS as superfícies reais do cômodo."""
        if self._gt_cache is not None and len(self._gt_cache) > 0:
            return self._gt_cache
        pts: List[np.ndarray] = []
        for pl in self.planes:
            nu = max(int(pl.umax / step), 1)
            nv = max(int(pl.vmax / step), 1)
            u = np.linspace(0, pl.umax, nu + 1)
            v = np.linspace(0, pl.vmax, nv + 1)
            # amostra apenas as faces rebatidas (não as faces internas do sólido)
            for uu in u:
                for vv in v:
                    pts.append(pl.origin + uu * pl.u_axis + vv * pl.v_axis)
        arr = np.asarray(pts)
        self._gt_cache = arr
        return arr


# --------------------------------------------------------------------------
# Malhas auxiliares (roteador, eixos, grade métrica)
# --------------------------------------------------------------------------
def box_triangles(center: np.ndarray, half: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Caixa como (vertices (8,3), faces (12,3))."""
    c, h = np.asarray(center, float), np.asarray(half, float)
    v = np.array([[sx, sy, sz] for sz in (-1, 1) for sy in (-1, 1) for sx in (-1, 1)],
                 dtype=np.float64) * h + c
    f = np.array([
        [0, 1, 3], [0, 3, 2],   # -z
        [4, 6, 7], [4, 7, 5],   # +z
        [0, 4, 5], [0, 5, 1],   # -y
        [2, 3, 7], [2, 7, 6],   # +y
        [0, 2, 6], [0, 6, 4],   # -x
        [1, 5, 7], [1, 7, 3],   # +x
    ], dtype=np.int32)
    return v, f


def merge_meshes(meshes) -> Tuple[np.ndarray, np.ndarray]:
    """Concatena (vertices, faces) de várias malhas."""
    verts, faces, off = [], [], 0
    for v, f in meshes:
        verts.append(v)
        faces.append(np.asarray(f, np.int32) + off)
        off += len(v)
    return np.vstack(verts), np.vstack(faces)


def router_model() -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Modelo 3D estilizado do roteador na origem.

    Retorna (vertices, faces, cor_por_vertice) em RGBA base (a cor final é
    definida no viewer). Corpo + 4 antenas + base.
    """
    body = box_triangles((0, 0, 0), (0.115, 0.085, 0.022))
    base = box_triangles((0, 0, -0.030), (0.100, 0.070, 0.010))
    parts = [body, base]
    for sx in (-0.085, 0.085):
        for sy in (-0.060, 0.060):
            ant = box_triangles((sx, sy, 0.085), (0.007, 0.007, 0.085))
            parts.append(ant)
    v, f = merge_meshes(parts)
    n_body = len(body[0])
    n_base = n_body + len(base[0])
    colors = np.zeros((len(v), 4), dtype=np.float32)
    colors[:n_body] = (0.05, 0.06, 0.08, 1.0)          # corpo escuro
    colors[n_body:n_base] = (0.10, 0.11, 0.14, 1.0)    # base
    colors[n_base:] = (0.00, 0.90, 1.00, 1.0)          # antenas neon
    return v.astype(np.float32), f, colors


def grid_segments(room: Room, step: float = 1.0, pad: float = 0.0
                  ) -> Tuple[np.ndarray, np.ndarray]:
    """Grade métrica do piso (segmentos) + segmentos das arestas do cômodo."""
    (x0, x1), (y0, y1) = room.cfg.xlim, room.cfg.ylim
    z = room.cfg.floor_z
    x0, x1, y0, y1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
    segs_minor, segs_border = [], []
    xs = np.arange(np.ceil(x0 / step) * step, x1 + 1e-9, step)
    ys = np.arange(np.ceil(y0 / step) * step, y1 + 1e-9, step)
    for x in xs:
        segs_minor.append([[x, y0, z], [x, y1, z]])
    for y in ys:
        segs_minor.append([[x0, y, z], [x1, y, z]])
    # contorno do cômodo
    for a, b in [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                 ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]:
        segs_minor.append([[a[0], a[1], z], [b[0], b[1], z]])
    del segs_border
    return (np.asarray(segs_minor, dtype=np.float32),
            np.asarray(segs_minor, dtype=np.float32))


def axis_arrows(length: float = 2.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Eixos cartesianos + pontas de seta.

    Retorna (segmentos (6,3), vertices_setas (V,3), faces_setas (F,3)).
    """
    segs = np.array([
        [0, 0, 0], [length, 0, 0],
        [0, 0, 0], [0, length, 0],
        [0, 0, 0], [0, 0, length],
    ], dtype=np.float32)
    verts, faces, off = [], [], 0
    for ax in range(3):
        d = np.zeros(3)
        d[ax] = 1.0
        h = length * 0.82
        tip = d * length
        o1 = np.zeros(3)
        o1[(ax + 1) % 3] = 0.055
        o2 = np.zeros(3)
        o2[(ax + 2) % 3] = 0.055
        v = np.array([d * h + o1, d * h - o1, d * h + o2, d * h - o2, tip],
                     dtype=np.float64)
        f = np.array([[0, 2, 4], [2, 1, 4], [1, 3, 4], [3, 0, 4],
                      [0, 1, 2], [0, 3, 1]], dtype=np.int32) + off
        verts.append(v.astype(np.float32))
        faces.append(f)
        off += len(v)
    return segs, np.vstack(verts), np.vstack(faces)


def ground_truth_room_mesh(room: Room) -> Tuple[np.ndarray, np.ndarray]:
    """Casca do cômodo real (piso+teto+4 paredes) para sobreposição visual."""
    x0, x1 = room.cfg.xlim
    y0, y1 = room.cfg.ylim
    z0, z1 = room.cfg.zlim
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]],
                 dtype=np.float32)
    f = np.array([[0, 1, 2], [0, 2, 3],    # piso
                  [4, 6, 5], [4, 7, 6],    # teto
                  [0, 4, 5], [0, 5, 1],
                  [1, 5, 6], [1, 6, 2],
                  [2, 6, 7], [2, 7, 3],
                  [3, 7, 4], [3, 4, 0]], dtype=np.int32)
    return v, f


def plane_distance(points: np.ndarray, room: Room) -> np.ndarray:
    """Distância de cada ponto à superfície real mais próxima do cômodo."""
    if len(points) == 0:
        return np.zeros(0)
    d = np.full(len(points), np.inf)
    for pl in room.planes:
        rel = points - pl.origin
        a = rel @ pl.u_axis
        b = rel @ pl.v_axis
        c = rel @ pl.normal
        a_c = np.clip(a, pl.umin, pl.umax)
        b_c = np.clip(b, pl.vmin, pl.vmax)
        dist = np.sqrt((a - a_c) ** 2 + (b - b_c) ** 2 + c ** 2)
        d = np.minimum(d, dist)
    return d
