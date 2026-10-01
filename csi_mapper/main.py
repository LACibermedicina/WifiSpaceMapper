"""
Aplicação PySide6: janela principal, painel de ferramentas de câmera,
botão central "MAPEAR ESPAÇO" e execução do pipeline em QThread.
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from typing import List, Optional

import numpy as np

from PySide6 import QtCore, QtGui, QtWidgets

# O backend do VisPy precisa ser resolvido antes de criar o primeiro canvas.
try:  # pragma: no cover
    from vispy import app as vispy_app
except Exception:  # pragma: no cover
    vispy_app = None  # type: ignore

from .colors import colormap_lut
from .config import AppConfig, default_config
from .csi_source import CSISource, MockCSISource, make_source
from .dsp import MappingPipeline, MappingResult
from .reconstruction import ReconstructionResult, reconstruct
from .room import Room
from .viewer import MODE_POINTS, MODE_SOLID, MODE_WIRE, Viewport3D

APP_QSS = """
QMainWindow, QWidget { background: #0d1117; color: #d7dee8;
                       font-family: 'Segoe UI', 'Inter', 'DejaVu Sans'; }
QToolBar { background: #111823; border: 0px; padding: 6px; spacing: 6px; }
QToolButton { background: #1b2534; border: 1px solid #26374d; border-radius: 6px;
              padding: 6px 10px; color: #cfe0f5; }
QToolButton:hover { background: #24334a; border-color: #3d5b82; }
QToolButton:checked { background: #12507a; border-color: #37a6e0; color: #ffffff; }
QGroupBox { border: 1px solid #223044; border-radius: 8px; margin-top: 14px;
            padding: 10px 8px 8px 8px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px;
                   color: #7fb6e8; }
QPushButton { background: #1b2534; border: 1px solid #26374d; border-radius: 6px;
              padding: 6px 10px; color: #cfe0f5; }
QPushButton:hover { background: #24334a; }
QPushButton#mapButton { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                       stop:0 #12b76a, stop:1 #0a8f52); color: #ffffff;
                       font-size: 15px; font-weight: 700; padding: 11px 22px;
                       border: 1px solid #17d37d; border-radius: 8px; }
QPushButton#mapButton:hover { background: #16d07c; }
QPushButton#mapButton:disabled { background: #24402f; color: #7d9c8a;
                                 border-color: #2c4c39; }
QPushButton#cancelButton { background: #3a1c22; border-color: #6b2c36; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit { background: #131c29;
              border: 1px solid #26374d; border-radius: 5px; padding: 4px 6px;
              color: #d7dee8; }
QCheckBox { padding: 2px; }
QProgressBar { border: 1px solid #26374d; border-radius: 6px; text-align: center;
               background: #131c29; color: #d7dee8; height: 18px; }
QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                     stop:0 #1b9aaa, stop:1 #37a6e0); border-radius: 5px; }
QTextBrowser { background: #0f1720; border: 1px solid #1f2c3d;
               border-radius: 6px; }
QLabel#hint { color: #7d8ea3; font-size: 11px; }
QLabel#badgeMock { background: #3d2f0c; color: #ffd166; border: 1px solid #6b551a;
                   border-radius: 5px; padding: 3px 8px; font-weight: 700; }
QLabel#badgeHw { background: #0c2f3d; color: #66e0ff; border: 1px solid #1a5c73;
                 border-radius: 5px; padding: 3px 8px; font-weight: 700; }
QStatusBar { color: #9fb0c4; }
"""


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
class MappingWorker(QtCore.QThread):
    """Calibração + varredura + reconstrução em thread separada."""

    progress = QtCore.Signal(str, float, str)
    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, cfg: AppConfig, room: Room, source: CSISource,
                 pace: bool, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.room = room
        self.source = source
        self.pace = pace
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D102
        try:
            pipe = MappingPipeline(
                self.cfg, self.room, self.source,
                on_progress=lambda ph, f, m: self.progress.emit(ph, f, m),
                pace=self.pace, cancel=lambda: self._cancel)
            result: MappingResult = pipe.run()
            if self._cancel:
                return
            self.progress.emit("reconstrucao", 0.05,
                               "Filtrando nuvem (voxel/SOR) e agrupando (DBSCAN)…")
            self.msleep(60)
            rec: ReconstructionResult = reconstruct(result.points,
                                                    result.strengths,
                                                    self.cfg, self.room)
            self.progress.emit("reconstrucao", 1.0,
                               "Superfície gerada "
                               f"({rec.method}, {len(rec.triangles)} triângulos)")
            self.done.emit((result, rec))
        except Exception:  # pragma: no cover
            self.failed.emit(traceback.format_exc())


# --------------------------------------------------------------------------
# Janela principal
# --------------------------------------------------------------------------
class MainWindow(QtWidgets.QMainWindow):

    def __init__(self, cfg: AppConfig, pace: bool = True):
        super().__init__()
        self.cfg = cfg
        self.room = Room(cfg.room)
        self.pace = pace
        self.source: Optional[CSISource] = None
        self.worker: Optional[MappingWorker] = None
        self.result: Optional[MappingResult] = None
        self.recons: Optional[ReconstructionResult] = None

        self.setWindowTitle("Mapeador 3D de Espaço por CSI Wi-Fi  —  "
                            "Roteador na origem (0,0,0)")
        self.resize(1560, 940)

        self.viewport = Viewport3D(self.room)
        self.viewport.statusMessage.connect(self._on_viewport_status)

        self._build_toolbar()
        self._build_panel()
        self._build_statusbar()

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        split.addWidget(self.viewport)
        split.addWidget(self.panel_scroll)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([1120, 430])
        self.setCentralWidget(split)

        self._wire_shortcuts()
        self._select_source(first_time=True)

    # ------------------------------------------------------------------
    # Interface
    # ------------------------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QtWidgets.QToolBar("Principal")
        tb.setMovable(False)
        tb.setIconSize(QtCore.QSize(18, 18))
        self.addToolBar(tb)

        self.btn_map = QtWidgets.QPushButton("▶  MAPEAR ESPAÇO")
        self.btn_map.setObjectName("mapButton")
        self.btn_map.setToolTip("Executa calibração (0-3 s), varredura (3-10 s) "
                                "e geração/rendereização 3D automática")
        self.btn_map.clicked.connect(self.start_mapping)
        tb.addWidget(self.btn_map)

        self.btn_cancel = QtWidgets.QPushButton("■  Cancelar")
        self.btn_cancel.setObjectName("cancelButton")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_mapping)
        tb.addWidget(self.btn_cancel)
        tb.addSeparator()

        act_reset = QtGui.QAction("⤾ Resetar câmera", self)
        act_reset.setToolTip("Centraliza a câmera no roteador (vista isométrica)")
        act_reset.triggered.connect(self.viewport.reset_camera)
        tb.addAction(act_reset)

        self.view_actions: List[QtGui.QAction] = []
        for name, label, key in (("top", "⊞ Topo", "F2"),
                                 ("front", "▤ Frente", "F3"),
                                 ("side", "▥ Lado", "F4"),
                                 ("iso", "◇ Isométrica", "F5")):
            act = QtGui.QAction(label, self)
            act.setShortcut(key)
            act.setToolTip(f"Vista {label.split()[-1]} (planta/ortográfica rápida)")
            act.triggered.connect(lambda _=False, n=name: self.viewport.apply_view(n))
            tb.addAction(act)
            self.view_actions.append(act)
        tb.addSeparator()

        tb.addWidget(QtWidgets.QLabel(" Estilo: "))
        self.combo_mode = QtWidgets.QComboBox()
        self.combo_mode.addItems(["Nuvem de Pontos", "Wireframe (aramado)",
                                  "Malha Sólida (paredes)"])
        self.combo_mode.setCurrentIndex(0)
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        self.combo_mode.setToolTip("Alterna o estilo de visualização da cena")
        tb.addWidget(self.combo_mode)

        tb.addSeparator()
        self.btn_walk = QtWidgets.QPushButton("🚶 Navegação Livre (WASD)")
        self.btn_walk.setCheckable(True)
        self.btn_walk.setToolTip("Primeira pessoa: W/A/S/D anda, E/Q sobe/desce")
        self.btn_walk.toggled.connect(self._on_walk_toggled)
        tb.addWidget(self.btn_walk)

        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                             QtWidgets.QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.badge = QtWidgets.QLabel("—")
        self.badge.setObjectName("badgeMock")
        tb.addWidget(self.badge)

    def _build_panel(self) -> None:
        self.panel_scroll = QtWidgets.QScrollArea()
        self.panel_scroll.setWidgetResizable(True)
        self.panel_scroll.setMinimumWidth(380)
        inner = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(inner)
        v.setSpacing(10)

        # ---------------- fonte de dados ----------------
        gb_src = QtWidgets.QGroupBox("Fonte de dados CSI")
        f = QtWidgets.QFormLayout(gb_src)
        self.combo_src = QtWidgets.QComboBox()
        self.combo_src.addItems(["Automático (hardware → simulação)",
                                 "Simulação (Mock / Demonstração)",
                                 "UDP (hardware real)",
                                 "Serial (hardware real)"])
        self.combo_src.currentIndexChanged.connect(self._select_source)
        f.addRow("Modo:", self.combo_src)
        self.spin_udp = QtWidgets.QSpinBox()
        self.spin_udp.setRange(1, 65535)
        self.spin_udp.setValue(self.cfg.udp_port)
        f.addRow("Porta UDP:", self.spin_udp)
        self.edit_serial = QtWidgets.QLineEdit(self.cfg.serial_port)
        self.edit_serial.setPlaceholderText("/dev/ttyUSB0 ou COM5")
        f.addRow("Porta serial:", self.edit_serial)
        self.lbl_src = QtWidgets.QLabel("—")
        self.lbl_src.setObjectName("hint")
        self.lbl_src.setWordWrap(True)
        f.addRow(self.lbl_src)
        v.addWidget(gb_src)

        # ---------------- câmera ----------------
        gb_cam = QtWidgets.QGroupBox("Câmera e navegação")
        fc = QtWidgets.QVBoxLayout(gb_cam)
        for name, label in (("top", "Topo (planta baixa)"), ("front", "Frente"),
                            ("side", "Lado"), ("iso", "Isométrica")):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _=False, n=name: self.viewport.apply_view(n))
            fc.addWidget(b)
        b = QtWidgets.QPushButton("⟲ Centralizar no roteador / Resetar câmera")
        b.clicked.connect(self.viewport.reset_camera)
        fc.addWidget(b)
        self.chk_walk = QtWidgets.QCheckBox("Modo navegação livre (W/A/S/D + E/Q)")
        self.chk_walk.toggled.connect(self._on_walk_toggled)
        fc.addWidget(self.chk_walk)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Tamanho dos pontos:"))
        self.slider_pts = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider_pts.setRange(1, 16)
        self.slider_pts.setValue(5)
        self.slider_pts.valueChanged.connect(
            lambda val: self.viewport.set_point_size(float(val)))
        row.addWidget(self.slider_pts)
        fc.addLayout(row)
        for label, attr, checked in (
                ("Mostrar grade métrica do piso", "grid", True),
                ("Mostrar eixos X/Y/Z e setas", "axes", True),
                ("Mostrar roteador na origem", "router", True),
                ("Mostrar trajetória de varredura", "traj", True),
                ("Mostrar sala real (referência)", "gt", False)):
            chk = QtWidgets.QCheckBox(label)
            chk.setChecked(checked)
            chk.toggled.connect(
                lambda c, a=attr: self._toggle_reference(a, c))
            setattr(self, f"chk_{attr}", chk)
            fc.addWidget(chk)
        v.addWidget(gb_cam)

        # ---------------- espectro AoA ----------------
        gb_spec = QtWidgets.QGroupBox("Espectro AoA (MUSIC) — calibração")
        vs = QtWidgets.QVBoxLayout(gb_spec)
        self.lbl_spec = QtWidgets.QLabel("Execute \"Mapear Espaço\" para ver o "
                                         "espectro azimute × elevação.")
        self.lbl_spec.setObjectName("hint")
        self.lbl_spec.setAlignment(QtCore.Qt.AlignCenter)
        self.lbl_spec.setMinimumHeight(150)
        self.lbl_spec.setScaledContents(False)
        vs.addWidget(self.lbl_spec)
        v.addWidget(gb_spec)

        # ---------------- estatísticas ----------------
        gb_st = QtWidgets.QGroupBox("Estatísticas do mapeamento")
        vst = QtWidgets.QVBoxLayout(gb_st)
        self.txt_stats = QtWidgets.QTextBrowser()
        self.txt_stats.setMinimumHeight(230)
        self.txt_stats.setHtml(self._stats_html(None, None))
        vst.addWidget(self.txt_stats)
        v.addWidget(gb_st)

        # ---------------- log ----------------
        gb_log = QtWidgets.QGroupBox("Log")
        vl = QtWidgets.QVBoxLayout(gb_log)
        self.txt_log = QtWidgets.QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(400)
        self.txt_log.setMinimumHeight(120)
        vl.addWidget(self.txt_log)
        v.addWidget(gb_log)

        v.addStretch(1)
        self.panel_scroll.setWidget(inner)

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self.lbl_phase = QtWidgets.QLabel("Pronto.")
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(320)
        sb.addWidget(self.lbl_phase, 1)
        sb.addPermanentWidget(self.progress)

    def _wire_shortcuts(self) -> None:
        for key, name in (("P", MODE_POINTS), ("W", MODE_WIRE), ("M", MODE_SOLID)):
            sc = QtGui.QShortcut(QtGui.QKeySequence(key), self.viewport)
            sc.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda n=name: self._set_mode_by_code(n))
        sc = QtGui.QShortcut(QtGui.QKeySequence("Space"), self.viewport)
        sc.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        sc.activated.connect(self.viewport.reset_camera)

    # ------------------------------------------------------------------
    # Estado / utilidades
    # ------------------------------------------------------------------
    def _log(self, msg: str) -> None:
        self.txt_log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def _on_viewport_status(self, msg: str) -> None:
        self.lbl_phase.setText(msg)

    def _toggle_reference(self, attr: str, checked: bool) -> None:
        if attr == "grid":
            self.viewport.show_grid(checked)
        elif attr == "axes":
            self.viewport.show_axes(checked)
        elif attr == "router":
            self.viewport.show_router(checked)
        elif attr == "traj":
            self.viewport.show_trajectory(checked)
        elif attr == "gt":
            self.viewport.show_reference_room(checked)

    def _on_mode_changed(self, idx: int) -> None:
        self.viewport.set_render_mode([MODE_POINTS, MODE_WIRE, MODE_SOLID][idx])

    def _set_mode_by_code(self, mode: str) -> None:
        idx = {MODE_POINTS: 0, MODE_WIRE: 1, MODE_SOLID: 2}[mode]
        self.combo_mode.setCurrentIndex(idx)
        self.lbl_phase.setText(f"Estilo de visualização: {self.combo_mode.currentText()}")

    def _on_walk_toggled(self, checked: bool) -> None:
        active = self.viewport.toggle_walkthrough(checked)
        if self.btn_walk.isChecked() != active:
            self.btn_walk.blockSignals(True)
            self.btn_walk.setChecked(active)
            self.btn_walk.blockSignals(False)
        if self.chk_walk.isChecked() != active:
            self.chk_walk.blockSignals(True)
            self.chk_walk.setChecked(active)
            self.chk_walk.blockSignals(False)

    # ------------------------------------------------------------------
    # Fonte de CSI
    # ------------------------------------------------------------------
    def _select_source(self, first_time: bool = False) -> None:
        idx = self.combo_src.currentIndex()
        self.cfg.udp_port = int(self.spin_udp.value())
        self.cfg.serial_port = self.edit_serial.text().strip()
        prefer = {0: "auto", 1: "mock", 2: "udp", 3: "serial"}[idx]
        self.cfg.source = prefer
        if self.source is not None:
            try:
                self.source.close()
            except Exception:
                pass
        try:
            self.source = make_source(self.cfg, prefer)
            desc = self.source.describe()
            hw = bool(getattr(self.source, "hardware", False))
            reason = getattr(self.source, "fallback_reason", "")
            if hw:
                self.badge.setText("HARDWARE ATIVO")
                self.badge.setObjectName("badgeHw")
            else:
                self.badge.setText("MODO DEMONSTRAÇÃO")
                self.badge.setObjectName("badgeMock")
            self.badge.setStyleSheet(APP_QSS)
            self.lbl_src.setText(desc + (f"\n⚠ {reason}" if reason else ""))
            if not first_time or prefer == "mock":
                self._log(f"Fonte de CSI: {desc}")
                if reason:
                    self._log(f"Fallback: {reason}")
        except Exception as exc:
            self.source = None
            self.lbl_src.setText(f"Falha ao abrir a fonte: {exc}")
            self.badge.setText("SEM FONTE")
            self._log(f"Erro na fonte CSI: {exc}")

    # ------------------------------------------------------------------
    # Fluxo "Mapear Espaço"
    # ------------------------------------------------------------------
    def start_mapping(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        if self.source is None:
            self._log("Nenhuma fonte de CSI disponível. Verifique a configuração.")
            return
        self.btn_map.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setValue(0)
        self.viewport.clear_map()
        self.viewport.set_trajectory(None)
        self.lbl_phase.setText("Iniciando mapeamento…")
        self._log("=== Iniciando mapeamento do espaço ===")
        self._log("Fase 1: calibração (0-3 s) — isola o sinal estático direto; "
                  "Fase 2: varredura (3-10 s) — ToF + AoA (MUSIC)")
        self.worker = MappingWorker(self.cfg, self.room, self.source,
                                    self.pace, self)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def cancel_mapping(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.lbl_phase.setText("Cancelando…")
            self._log("Cancelamento solicitado pelo usuário.")

    def _on_progress(self, phase: str, frac: float, msg: str) -> None:
        pct = int(round(frac * 100))
        if phase == "calibracao":
            self.progress.setValue(int(pct * 0.30))
            self.lbl_phase.setText(f"Calibração — {msg}")
        elif phase == "varredura":
            self.progress.setValue(30 + int(pct * 0.55))
            self.lbl_phase.setText(f"Varredura — {msg}")
        elif phase == "reconstrucao":
            self.progress.setValue(85 + int(pct * 0.15))
            self.lbl_phase.setText(f"Reconstrução — {msg}")
        else:
            self.progress.setValue(100)
            self.lbl_phase.setText(msg)
        self._log(msg)

    def _on_failed(self, detail: str) -> None:
        self.btn_map.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.lbl_phase.setText("Falha no mapeamento.")
        for line in detail.strip().splitlines()[-6:]:
            self._log(line)

    def _on_done(self, payload) -> None:
        result, rec = payload
        self.result, self.recons = result, rec
        self.btn_map.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress.setValue(100)

        self.viewport.set_map(rec.points, rec.point_colors, rec.vertices,
                              rec.triangles, rec.vertex_colors, rec.edges)
        self.viewport.set_trajectory(result.traj)
        self._draw_spectrum(result)
        self.txt_stats.setHtml(self._stats_html(result, rec))

        pct = rec.metrics.get("cover_surface_25cm", 0.0) * 100.0
        self.lbl_phase.setText(
            f"Mapa pronto — {len(rec.points)} pts / {len(rec.triangles)} triângulos "
            f"/ cobertura ~{pct:.0f}% (<25 cm)")
        self._log(f"Reconstrução: método={rec.method}, "
                  f"vértices={len(rec.vertices)}, triângulos={len(rec.triangles)}, "
                  f"clusters={len(rec.clusters)}")
        self._log("Controles: órbita = botão esquerdo | pan = botão direito ou "
                  "Shift+esquerdo | zoom = roda | F2-F5 = vistas | P/W/M = estilo")

    # ------------------------------------------------------------------
    # Relatórios
    # ------------------------------------------------------------------
    def _stats_html(self, result: Optional[MappingResult],
                    rec: Optional[ReconstructionResult]) -> str:
        rows: List[str] = []

        def row(k: str, v: str) -> None:
            rows.append(f"<tr><td style='color:#8fb4d9;padding:2px 8px 2px 0'>{k}"
                        f"</td><td style='color:#e6eef8'>{v}</td></tr>")

        if result is None:
            return ("<p style='color:#7d8ea3'>Nenhum mapeamento executado. "
                    "Clique em <b>Mapear Espaço</b> para reconstruir o cômodo "
                    "a partir do CSI.</p>")
        c = result.calib
        s = result.stats
        row("Fonte", self.source.describe() if self.source else "—")
        row("Antenas / subportadoras",
            f"{int(s.get('n_ant', 0))} / {self.cfg.radio.n_sub}")
        row("Frequência / largura de banda",
            f"{self.cfg.radio.f0/1e9:.3f} GHz / {self.cfg.radio.bandwidth/1e6:.0f} MHz")
        row("Calibração", c.describe() if c else "—")
        row("Pacotes usados", f"{int(c.packets_used) if c else 0}")
        row("Posições de varredura", f"{int(s.get('groups', 0))}")
        row("Percursos estimados (brutos)", f"{int(s.get('candidates', 0))}")
        row("Pontos aceitos", f"{int(s.get('accepted', 0))}")
        row("Rejeitados (fora do volume)", f"{int(s.get('rejected_out_of_bounds', 0))}")
        row("Rejeitados (percurso direto)", f"{int(s.get('rejected_direct_path', 0))}")
        row("Tempo total", f"{s.get('elapsed_s', 0.0):.2f} s")

        if rec is not None:
            m = rec.metrics
            row("— Reconstrução —", "")
            row("Método de superfície", rec.method)
            row("Pontos (bruto → filtrado)",
                f"{int(m.get('points_raw', 0))} → {int(len(rec.points))}")
            row("Clusters DBSCAN", f"{int(m.get('dbscan_clusters', 0))}")
            row("Vértices / triângulos",
                f"{int(m.get('vertices', 0))} / {int(m.get('triangles', 0))}")
            if "bbox_x" in m:
                row("Extensão mapeada (X×Y×Z)",
                    f"{m['bbox_x']:.2f} × {m['bbox_y']:.2f} × {m['bbox_z']:.2f} m")
            if "err_surface_mean_m" in m:
                row("Erro médio da malha",
                    f"{m['err_surface_mean_m']*100:.1f} cm")
                row("Cobertura da malha (<25 cm)",
                    f"{m.get('cover_surface_25cm', 0.0)*100:.1f}%")
            if "err_cloud_mean_m" in m:
                row("Erro médio da nuvem",
                    f"{m['err_cloud_mean_m']*100:.1f} cm")
                row("Cobertura da nuvem (<25 cm)",
                    f"{m.get('cover_cloud_25cm', 0.0)*100:.1f}%")
        row("Sala de referência",
            f"{self.cfg.room.size[0]:.1f} × {self.cfg.room.size[1]:.1f} × "
            f"{self.cfg.room.size[2]:.1f} m (roteador no centro)")

        return ("<table style='font-size:12px;border-collapse:collapse'>"
                + "".join(rows) + "</table>")

    def _draw_spectrum(self, result: MappingResult) -> None:
        """Renderiza o espectro AoA (azimute × elevação) como QImage."""
        c = result.calib
        if c is None or c.spectrum is None:
            return
        spec = np.asarray(c.spectrum, dtype=np.float64).T     # (el, az)
        if spec.size == 0:
            return
        db = 10.0 * np.log10(spec / max(spec.max(), 1e-30) + 1e-12)
        db = np.clip(db, -45.0, 0.0)
        t = (db + 45.0) / 45.0
        lut = colormap_lut(256)
        idx = np.clip((t * 255).astype(np.uint8), 0, 255)
        rgba = (lut[idx] * 255.0).astype(np.uint8)
        rgba = np.ascontiguousarray(np.flipud(rgba))
        h, w, _ = rgba.shape
        img = QtGui.QImage(rgba.data, w, h, 4 * w,
                           QtGui.QImage.Format_RGBA8888).copy()
        pix = QtGui.QPixmap.fromImage(img).scaled(
            380, 170, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        self.lbl_spec.setPixmap(pix)
        self.lbl_spec.setToolTip(
            "Espectro MUSIC 2D na etapa de calibração\n"
            f"Horizontal: azimute ({c.az_grid[0]:.0f}° a {c.az_grid[-1]:.0f}°)\n"
            f"Vertical: elevação ({c.el_grid[0]:.0f}° a {c.el_grid[-1]:.0f}°)\n"
            "Escala de cor: pseudospectro em dB (0 dB = pico)")

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            if self.worker is not None and self.worker.isRunning():
                self.worker.cancel()
                self.worker.wait(1500)
            if self.source is not None:
                self.source.close()
        except Exception:
            pass
        super().closeEvent(event)


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Mapeador 3D de espaço por CSI Wi-Fi (PySide6 + VisPy + Open3D)")
    p.add_argument("--source", choices=["auto", "mock", "udp", "serial"],
                   default="auto", help="fonte de CSI (padrão: auto)")
    p.add_argument("--udp-port", type=int, default=5566)
    p.add_argument("--serial-port", type=str, default="")
    p.add_argument("--fast", action="store_true",
                   help="desativa o ritmo de 3 s + 7 s (execução imediata)")
    p.add_argument("--room", type=str, default="4,5,2.8",
                   help="dimensões do cômodo: L,A,H (padrão 4,5,2.8)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts, True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    if vispy_app is not None:
        try:
            vispy_app.use_app("pyside6")
        except Exception:
            pass

    cfg = default_config()
    try:
        dims = [float(x) for x in args.room.split(",")]
        if len(dims) == 3:
            cfg.room.size = (dims[0], dims[1], dims[2])
    except ValueError:
        pass
    cfg.source = args.source
    cfg.udp_port = args.udp_port
    cfg.serial_port = args.serial_port

    app.setStyleSheet(APP_QSS)
    win = MainWindow(cfg, pace=not args.fast)
    win.show()
    win._log("Software de mapeamento 3D por CSI iniciado.")
    win._log("Dica: clique em 'MAPEAR ESPAÇO' para gerar o mapa 3D e depois "
             "explore com o mouse (órbita/pan/zoom).")
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
