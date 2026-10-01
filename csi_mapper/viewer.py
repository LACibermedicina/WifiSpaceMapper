"""
Viewport 3D interativo (VisPy) embutido em um widget Qt.

Câmeras
-------
* OrbitPanCamera  : órbita = botão esquerdo; PAN = botão direito ou
                    Shift + esquerdo; ZOOM = roda do mouse.
* WalkCamera      : primeira pessoa, W/A/S/D anda, E/Q sobe/desce.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PySide6 import QtCore, QtWidgets

try:  # vispy >= 0.13
    from vispy import keys
except Exception:  # pragma: no cover
    from vispy.util import keys  # type: ignore

from vispy import scene
from vispy.scene import visuals
from vispy.scene.cameras import FlyCamera, TurntableCamera
from vispy.util.quaternion import Quaternion

from .colors import COL_AXIS, COL_GRID, COL_GT_ROOM, COL_TRAJ
from .room import (Room, axis_arrows, grid_segments, ground_truth_room_mesh,
                   router_model)

Point3 = Sequence[float]


# --------------------------------------------------------------------------
# Câmeras
# --------------------------------------------------------------------------
class OrbitPanCamera(TurntableCamera):
    """Câmera turntable com pan no botão direito / Shift+esquerdo."""

    def __init__(self, **kwargs):
        kwargs.setdefault("fov", 45.0)
        kwargs.setdefault("elevation", 26.0)
        kwargs.setdefault("azimuth", -42.0)
        kwargs.setdefault("distance", 9.0)
        super().__init__(**kwargs)
        self.pan_speed = 1.0
        self.zoom_step = 0.10
        self.rot_step = 0.42
        self._rot_ref: Optional[Tuple[float, float]] = None
        self._pan_ref: Optional[np.ndarray] = None

    # ---------------- geometria da câmera ----------------
    @property
    def actual_distance(self) -> float:
        d = getattr(self, "_actual_distance", None)
        return float(d if d is not None else (self.distance or 6.0))

    def view_basis(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Vetores do mundo: (right, up, view_dir, center->eye_dir).

        view_dir aponta do olho para o centro (direção de observação);
        center->eye_dir aponta do centro para a câmera.
        """
        az = math.radians(self.azimuth)
        el = math.radians(self.elevation)
        center_to_eye = np.array([math.cos(el) * math.cos(az),
                                  math.cos(el) * math.sin(az),
                                  math.sin(el)], dtype=np.float64)
        view = -center_to_eye
        world_up = np.array([0.0, 0.0, 1.0])
        right = np.cross(view, world_up)
        n = float(np.linalg.norm(right))
        if n < 1e-9:                      # olhando exatamente para cima/baixo
            right = np.array([1.0, 0.0, 0.0])
        else:
            right /= n
        up = np.cross(right, view)
        nn = float(np.linalg.norm(up))
        up = up / nn if nn > 1e-9 else np.array([0.0, 1.0, 0.0])
        return right, up, view, center_to_eye

    def camera_position(self) -> np.ndarray:
        _, _, _, c2e = self.view_basis()
        return np.asarray(self.center, dtype=np.float64) + self.actual_distance * c2e

    # ---------------- interação ----------------
    def viewbox_mouse_event(self, event) -> None:  # noqa: C901
        if not self.interactive:
            super().viewbox_mouse_event(event)
            return
        et = event.type

        if et == "mouse_wheel":
            delta = float(event.delta[1])
            k = 1.0 + self.zoom_step
            self.distance = float(np.clip(self.actual_distance * (k ** -delta),
                                          0.20, 400.0))
            self.view_changed()
            event.handled = True
            return

        if et == "mouse_press":
            event.handled = True
            return

        if et in ("mouse_release", "mouse_double_click"):
            self._rot_ref = None
            self._pan_ref = None
            event.handled = True
            return

        if et != "mouse_move":
            return
        if event.press_event is None or not event.buttons:
            return

        buttons = list(event.buttons)
        mods = event.mouse_event.modifiers
        try:
            shift = keys.SHIFT in mods
        except TypeError:                      # pragma: no cover
            shift = "Shift" in str(mods)

        p1 = np.asarray(event.mouse_event.press_event.pos, dtype=np.float64)[:2]
        p2 = np.asarray(event.mouse_event.pos, dtype=np.float64)[:2]
        d = p2 - p1
        w, h = self._viewbox.size

        wants_pan = (2 in buttons) or (1 in buttons and shift)
        if wants_pan:
            right, up, _, _ = self.view_basis()
            if self._pan_ref is None:
                self._pan_ref = np.asarray(self.center, dtype=np.float64)
            span = 2.0 * self.actual_distance * math.tan(math.radians(self.fov) / 2.0)
            scale = span / max(float(h), 1.0) * self.pan_speed
            shift_vec = (-d[0] * scale) * right + (d[1] * scale) * up
            self.center = tuple(self._pan_ref + shift_vec)
            self.view_changed()
            event.handled = True
            return

        if 1 in buttons:
            if self._rot_ref is None:
                self._rot_ref = (self.azimuth, self.elevation)
            self.azimuth = self._rot_ref[0] - d[0] * self.rot_step
            self.elevation = float(np.clip(self._rot_ref[1] + d[1] * self.rot_step,
                                           -89.9, 89.9))
            self.view_changed()
            event.handled = True
            return

        if 3 in buttons:      # botão do meio = zoom por arrasto vertical
            if self._pan_ref is None:
                self._pan_ref = np.array([self.actual_distance, 0.0])
            zoom = math.exp(-(p2[1] - p1[1]) * 0.01)
            self.distance = float(np.clip(self._pan_ref[0] * zoom, 0.20, 400.0))
            self.view_changed()
            event.handled = True


class WalkCamera(FlyCamera):
    """Primeira pessoa: W/A/S/D move, E/Q sobe/desce."""

    def __init__(self, **kwargs):
        kwargs.setdefault("fov", 68.0)
        super().__init__(**kwargs)
        self.scale_factor = 1.8
        self.auto_roll = True
        self._keymap = {
            "W": (+1, 1), "S": (-1, 1),
            "A": (-1, 2), "D": (+1, 2),
            "E": (+1, 3), "Q": (-1, 3),
            keys.UP: (+1, 1), keys.DOWN: (-1, 1),
            keys.LEFT: (-1, 2), keys.RIGHT: (+1, 2),
            "R": (+1, 6), "F": (-1, 6),
            keys.SPACE: (0, 1, 2, 3),
        }

    def place_at(self, position: Point3, view_dir: Point3, up_hint: Point3) -> None:
        """Posiciona a câmera (vetor de visão + referência de 'cima')."""
        v = np.asarray(view_dir, dtype=np.float64)
        n = float(np.linalg.norm(v))
        if n < 1e-9:
            return
        v = v / n
        u = np.asarray(up_hint, dtype=np.float64)
        right = np.cross(v, u)
        nr = float(np.linalg.norm(right))
        if nr < 1e-9:
            right = np.array([1.0, 0.0, 0.0])
        else:
            right = right / nr
        up = np.cross(right, v)
        M = np.column_stack([right, up, -v])
        self.rotation1 = _quat_from_matrix(M)
        self.rotation2 = Quaternion()
        self._center = tuple(float(c) for c in position)
        self.view_changed()

    @property
    def eye(self) -> np.ndarray:
        return np.asarray(self.center, dtype=np.float64)


def _quat_from_matrix(m: np.ndarray) -> Quaternion:
    """Matriz de rotação (3x3) -> Quaternion do VisPy."""
    tr = float(m[0, 0] + m[1, 1] + m[2, 2])
    if tr > 0.0:
        s = math.sqrt(tr + 1.0) * 2.0
        w, x = 0.25 * s, (m[2, 1] - m[1, 2]) / s
        y, z = (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        w, x = (m[2, 1] - m[1, 2]) / s, 0.25 * s
        y, z = (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        w, x = (m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s
        y, z = 0.25 * s, (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        w, x = (m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s
        y, z = (m[1, 2] + m[2, 1]) / s, 0.25 * s
    return Quaternion(w, x, y, z)


# --------------------------------------------------------------------------
# Viewport
# --------------------------------------------------------------------------
VIEWS: Dict[str, Tuple[float, float]] = {          # nome -> (azimute, elevação)
    "iso": (-42.0, 26.0),
    "top": (-90.0, 88.0),
    "front": (-90.0, 2.0),
    "side": (0.0, 2.0),
}

MODE_POINTS = "points"
MODE_WIRE = "wire"
MODE_SOLID = "solid"


class Viewport3D(QtWidgets.QWidget):
    """Contêiner do SceneCanvas do VisPy com tudo que é exibido em 3D."""

    statusMessage = QtCore.Signal(str)

    def __init__(self, room: Room, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.room = room
        self.cfg = room.cfg
        self._mode = MODE_POINTS
        self._point_size = 5.0
        self._walking = False
        self._orbit_state: Optional[tuple] = None
        self._has_map = False

        self.canvas = scene.SceneCanvas(keys="interactive", bgcolor="#080b11",
                                        show=False, vsync=False)
        self.canvas.native.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.canvas.native.setMinimumSize(640, 420)
        self.canvas.native.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, True)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.canvas.native)

        self.view = self.canvas.central_widget.add_view()
        self.orbit = OrbitPanCamera()
        self.walk = WalkCamera()
        self.view.camera = self.orbit

        self._build_reference_visuals()
        self._build_map_visuals()
        # set_range() percorre os bounds de TODOS os visuais; com uma nuvem
        # ainda vazia ele pode falhar, então a vista é definida de forma
        # explícita a partir da geometria conhecida do cômodo.
        try:
            self.view.camera.set_range()
        except Exception:
            pass
        self.apply_view("iso")

    # ------------------------------------------------------------------
    # Construção dos visuais
    # ------------------------------------------------------------------
    def _build_reference_visuals(self) -> None:
        # grade métrica do piso
        segs, _ = grid_segments(self.room, step=1.0)
        self.grid = visuals.Line(pos=segs, connect="segments",
                                 color=COL_GRID, width=1.0, method="gl")
        self.view.add(self.grid)

        # arestas do cômodo (caixa de referência)
        v, f = ground_truth_room_mesh(self.room)
        self.room_edges = visuals.Line(pos=v[f].reshape(-1, 3), connect="segments",
                                       color=(0.35, 0.62, 0.95, 0.45), width=1.0,
                                       method="gl")
        self.view.add(self.room_edges)

        # sala real (referência translúcida) - desligada por padrão
        self.gt_room = visuals.Mesh(vertices=v.astype(np.float32),
                                    faces=f.astype(np.uint32),
                                    color=COL_GT_ROOM, shading=None)
        self.gt_room.visible = False
        self.view.add(self.gt_room)

        # eixos cartesianos coloridos + setas
        L = max(self.cfg.size) * 0.58
        segs, av, af = axis_arrows(L)
        self.axis_lines = []
        for i, key in enumerate(("x", "y", "z")):
            ln = visuals.Line(pos=segs[i * 2:i * 2 + 2].astype(np.float32),
                              color=COL_AXIS[key], width=2.5, method="gl")
            self.view.add(ln)
            self.axis_lines.append(ln)
        cols = np.zeros((len(av), 4), dtype=np.float32)
        for i, key in enumerate(("x", "y", "z")):
            cols[i * 6:(i + 1) * 6] = COL_AXIS[key]
        self.axis_heads = visuals.Mesh(vertices=av, faces=af,
                                       vertex_colors=cols, shading="flat")
        self.view.add(self.axis_heads)

        # roteador na origem
        rv, rf, rc = router_model()
        self.router = visuals.Mesh(vertices=rv, faces=rf, vertex_colors=rc,
                                   shading="smooth")
        self.view.add(self.router)
        self.router_glow = visuals.Markers()
        self.router_glow.set_data(pos=np.array([[0.0, 0.0, 0.0]], np.float32),
                                  face_color=(0.05, 0.85, 1.0, 0.20),
                                  edge_color=None, size=26.0)
        self.view.add(self.router_glow)

        # rótulos de escala (a cada metro nos eixos X e Y)
        try:
            pos, txt = [], []
            (x0, x1), (y0, y1) = self.cfg.xlim, self.cfg.ylim
            z = self.cfg.floor_z
            for x in np.arange(np.ceil(x0), x1 + 1e-9, 1.0):
                if abs(x) < 1e-9:
                    continue
                pos.append([x, y0, z]); txt.append(f"x={x:+.0f} m")
            for y in np.arange(np.ceil(y0), y1 + 1e-9, 1.0):
                if abs(y) < 1e-9:
                    continue
                pos.append([x0, y, z]); txt.append(f"y={y:+.0f} m")
            self.ticks = visuals.Text(pos=np.asarray(pos, np.float32), text=txt,
                                      color=(0.66, 0.72, 0.80, 0.85),
                                      font_size=7.5, anchor_x="center",
                                      anchor_y="center")
            self.view.add(self.ticks)
        except Exception:  # pragma: no cover - fonte/GL indisponível
            self.ticks = None

    def _build_map_visuals(self) -> None:
        # IMPORTANTE: o VisPy calcula bounds percorrendo todos os visuais.
        # Visuais com array vazio quebram set_range(), então cada um nasce com
        # geometria degenerada (um ponto) e cor totalmente transparente.
        self.pcloud = visuals.Markers()
        self.pcloud.set_data(pos=np.zeros((1, 3), np.float32),
                             face_color=(0.0, 0.0, 0.0, 0.0),
                             size=self._point_size)
        self.view.add(self.pcloud)

        # malha sólida: nasce com um triângulo degenerado (sem dados)
        self.mesh_solid = visuals.Mesh(vertices=np.zeros((3, 3), np.float32),
                                       faces=np.array([[0, 1, 2]], np.uint32),
                                       color=(0.0, 0.0, 0.0, 0.0), shading="smooth")
        self.mesh_solid.visible = False
        self.view.add(self.mesh_solid)

        self.mesh_wire = visuals.Line(pos=np.zeros((2, 3), np.float32),
                                      connect="segments",
                                      color=(0.0, 0.0, 0.0, 0.0), width=1.0,
                                      method="gl")
        self.view.add(self.mesh_wire)

        self.traj = visuals.Line(pos=np.zeros((2, 3), np.float32),
                                 color=(0.0, 0.0, 0.0, 0.0), width=2.0,
                                 method="gl")
        self.view.add(self.traj)
        self.traj_pts = visuals.Markers()
        self.traj_pts.set_data(pos=np.zeros((1, 3), np.float32),
                               face_color=(0.0, 0.0, 0.0, 0.0),
                               edge_color=None, size=4.0)
        self.view.add(self.traj_pts)

    # ------------------------------------------------------------------
    # Câmera: vistas e reset
    # ------------------------------------------------------------------
    def _fit_distance(self) -> float:
        diag = float(np.linalg.norm(self.cfg.size))
        return float(np.clip(diag * 1.05, 3.0, 200.0))

    def apply_view(self, name: str) -> None:
        """Aplica vista pré-definida (iso|top|front|side), centrada no roteador."""
        az, el = VIEWS.get(name, VIEWS["iso"])
        cam = self.orbit
        cam.center = (0.0, 0.0, 0.0)
        cam.azimuth = az
        cam.elevation = el
        cam.distance = self._fit_distance()
        cam.view_changed()
        self.statusMessage.emit(f"Vista '{name}' aplicada (centro no roteador)")

    def reset_camera(self) -> None:
        """Resetar câmera / centralizar no roteador."""
        if self._walking:
            self.toggle_walkthrough(False)
        cam = self.orbit
        cam.center = (0.0, 0.0, 0.0)
        cam.fov = 45.0
        cam.azimuth, cam.elevation = VIEWS["iso"]
        cam.distance = self._fit_distance()
        cam.view_changed()
        self.statusMessage.emit("Câmera centralizada no roteador (0,0,0)")

    # ------------------------------------------------------------------
    # Modo de navegação livre (primeira pessoa)
    # ------------------------------------------------------------------
    def toggle_walkthrough(self, enabled: bool) -> bool:
        """Liga/desliga a navegação livre (W/A/S/D + E/Q)."""
        if enabled == self._walking:
            return self._walking
        self._walking = enabled
        if enabled:
            cam = self.orbit
            self._orbit_state = (cam.center, cam.azimuth, cam.elevation,
                                 cam.distance, cam.fov)
            right, up, view, c2e = cam.view_basis()
            eye = cam.camera_position()
            # garante que a câmera fique dentro do volume navegável
            (x0, x1), (y0, y1), (z0, z1) = (self.cfg.xlim, self.cfg.ylim,
                                            self.cfg.zlim)
            eye = np.clip(eye, [x0 + 0.15, y0 + 0.15, z0 + 1.2],
                          [x1 - 0.15, y1 - 0.15, z1 - 0.15])
            self.view.camera = self.walk
            self.walk.place_at(eye, view, up)
            self.canvas.native.setFocus()
            self.statusMessage.emit(
                "Navegação livre ativa — W/A/S/D anda, E/Q sobe/desce, "
                "arraste com o esquerdo para olhar")
        else:
            self.view.camera = self.orbit
            if self._orbit_state is not None:
                c, az, el, dist, fov = self._orbit_state
                self.orbit.center = c
                self.orbit.azimuth = az
                self.orbit.elevation = el
                self.orbit.distance = dist
                self.orbit.fov = fov
                self.orbit.view_changed()
            self.statusMessage.emit("Navegação orbital ativa "
                                    "(órbita: esquerdo, pan: direito)")
        self._apply_mode_visibility()
        return self._walking

    @property
    def walking(self) -> bool:
        return self._walking

    # ------------------------------------------------------------------
    # Estilo de visualização
    # ------------------------------------------------------------------
    def set_render_mode(self, mode: str) -> None:
        if mode not in (MODE_POINTS, MODE_WIRE, MODE_SOLID):
            return
        self._mode = mode
        self._apply_mode_visibility()

    @property
    def render_mode(self) -> str:
        return self._mode

    def _apply_mode_visibility(self) -> None:
        has = self._has_map
        if self._mode == MODE_POINTS:
            self.pcloud.visible = True
            self.mesh_solid.visible = False
            self.mesh_wire.visible = False
        elif self._mode == MODE_WIRE:
            self.pcloud.visible = False
            self.mesh_solid.visible = False
            self.mesh_wire.visible = has
        else:
            self.pcloud.visible = False
            self.mesh_solid.visible = has
            self.mesh_wire.visible = False
        self.canvas.update()

    def set_point_size(self, size: float) -> None:
        self._point_size = float(size)
        self.pcloud.set_data(size=self._point_size)
        self.canvas.update()

    def show_grid(self, on: bool) -> None:
        self.grid.visible = bool(on)
        if self.ticks is not None:
            self.ticks.visible = bool(on)
        self.canvas.update()

    def show_axes(self, on: bool) -> None:
        for ln in self.axis_lines:
            ln.visible = bool(on)
        self.axis_heads.visible = bool(on)
        self.canvas.update()

    def show_router(self, on: bool) -> None:
        self.router.visible = bool(on)
        self.router_glow.visible = bool(on)
        self.canvas.update()

    def show_trajectory(self, on: bool) -> None:
        self.traj.visible = bool(on)
        self.traj_pts.visible = bool(on)
        self.canvas.update()

    def show_reference_room(self, on: bool) -> None:
        self.gt_room.visible = bool(on)
        self.canvas.update()

    # ------------------------------------------------------------------
    # Dados do mapa
    # ------------------------------------------------------------------
    def clear_map(self) -> None:
        self.pcloud.set_data(pos=np.zeros((1, 3), np.float32),
                             face_color=(0.0, 0.0, 0.0, 0.0))
        self.mesh_solid.set_data(vertices=np.zeros((3, 3), np.float32),
                                 faces=np.array([[0, 1, 2]], np.uint32),
                                 color=(0.0, 0.0, 0.0, 0.0))
        self.mesh_solid.visible = False
        self.mesh_wire.set_data(pos=np.zeros((2, 3), np.float32),
                                connect="segments",
                                color=(0.0, 0.0, 0.0, 0.0))
        self.mesh_wire.visible = False
        self._has_map = False
        self.canvas.update()

    def set_map(self, points: np.ndarray, point_colors: np.ndarray,
                vertices: np.ndarray, triangles: np.ndarray,
                vertex_colors: np.ndarray, edges: np.ndarray) -> None:
        """Publica nuvem de pontos + malha na cena."""
        pts = np.asarray(points, dtype=np.float32)
        self.pcloud.set_data(pos=pts,
                             face_color=(np.asarray(point_colors, np.float32)
                                         if len(point_colors) == len(pts)
                                         else (0.2, 0.6, 1.0, 0.9)),
                             size=self._point_size, edge_width=0.0)

        verts = np.asarray(vertices, dtype=np.float32)
        tris = np.asarray(triangles, dtype=np.uint32)
        if len(tris) > 0 and len(verts) > 0:
            cols = (np.asarray(vertex_colors, np.float32)
                    if len(vertex_colors) == len(verts)
                    else (0.35, 0.75, 1.0, 1.0))
            self.mesh_solid.set_data(vertices=verts, faces=tris,
                                     vertex_colors=cols, shading="smooth")
            e = np.asarray(edges, dtype=np.int64)
            if len(e) > 0:
                seg = verts[e].reshape(-1, 3)
                self.mesh_wire.set_data(pos=seg, connect="segments",
                                        color=(0.30, 0.85, 1.00, 0.85))
                self.mesh_wire.visible = (self._mode == MODE_WIRE)
            self._has_map = True
        else:
            self.mesh_wire.set_data(pos=np.zeros((2, 3), np.float32),
                                    connect="segments",
                                    color=(0.0, 0.0, 0.0, 0.0))
            self._has_map = False
        self._apply_mode_visibility()
        self.canvas.update()

    def set_trajectory(self, traj: np.ndarray) -> None:
        if traj is None or len(traj) < 2:
            self.traj.set_data(pos=np.zeros((2, 3), np.float32),
                               color=(0.0, 0.0, 0.0, 0.0))
            self.traj_pts.set_data(pos=np.zeros((1, 3), np.float32),
                                   face_color=(0.0, 0.0, 0.0, 0.0))
        else:
            t = np.asarray(traj, dtype=np.float32)
            self.traj.set_data(pos=t, color=COL_TRAJ)
            self.traj_pts.set_data(pos=t, face_color=(1.0, 0.75, 0.15, 0.45))
        self.canvas.update()

    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        try:
            self.canvas.update()
        except Exception:  # pragma: no cover
            pass
