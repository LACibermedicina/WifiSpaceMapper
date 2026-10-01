"""
Verificação da interface gráfica em modo offscreen (sem monitor).

    QT_QPA_PLATFORM=offscreen python gui_shot.py

Executa o mapeamento completo, alterna os estilos de visualização e salva
capturas em ./screenshots/.
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6 import QtCore, QtWidgets
from vispy import app as vispy_app
from vispy.io import write_png

from csi_mapper.config import default_config
from csi_mapper.main import APP_QSS, MainWindow

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")


def pump(app, seconds: float = 0.15) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    try:
        vispy_app.use_app("pyside6")
    except Exception as exc:
        print("use_app:", exc)
    app.setStyleSheet(APP_QSS)

    cfg = default_config()
    cfg.source = "mock"
    win = MainWindow(cfg, pace=False)
    win.combo_src.setCurrentIndex(1)          # simulação explícita
    win.resize(1560, 940)
    win.show()
    pump(app, 0.4)

    print("Iniciando mapeamento (mock, sem ritmo)...")
    t0 = time.time()
    win.start_mapping()
    while win.worker is not None and win.worker.isRunning() and time.time() - t0 < 300:
        app.processEvents()
        time.sleep(0.02)
    pump(app, 1.0)
    print(f"Mapeamento concluído em {time.time() - t0:.1f} s")

    results = {}

    def shot(name: str, mode_idx: int, view: str = "iso") -> None:
        win.combo_mode.setCurrentIndex(mode_idx)
        win.viewport.apply_view(view)
        pump(app, 0.6)
        path = os.path.join(OUT, f"{name}.png")
        ok = False
        try:
            arr = win.viewport.canvas.render()
            if arr is not None and arr.size:
                write_png(path, arr)
                results[name] = ("canvas", float(np.asarray(arr).mean()), arr.shape)
                ok = True
        except Exception as exc:
            print(f"  canvas.render falhou ({name}): {type(exc).__name__}: {exc}")
        if not ok:
            try:
                pm = win.viewport.canvas.native.grabFramebuffer()
                pm.save(path)
                img = pm.toImage()
                vals = [img.pixelColor(x, y).getRgb()[0]
                        for x in range(0, img.width(), 40)
                        for y in range(0, img.height(), 40)]
                results[name] = ("grabFramebuffer", float(np.mean(vals)), (img.height(), img.width(), 4))
                ok = True
            except Exception as exc:
                print(f"  grabFramebuffer falhou ({name}): {type(exc).__name__}: {exc}")
        print(f"  {name}: {'OK' if ok else 'FALHOU'} -> {results.get(name)}")

    shot("01_pontos_vista_topo", 0, "top")
    shot("02_pontos_iso", 0, "iso")
    shot("03_wireframe", 1, "iso")
    shot("04_malha_solida", 2, "iso")
    shot("05_malha_solida_lado", 2, "side")
    win.viewport.show_reference_room(True)
    shot("06_solida_com_sala_referencia", 2, "iso")

    # janela completa (inclui painéis laterais Qt)
    win.viewport.show_reference_room(False)
    win.combo_mode.setCurrentIndex(2)
    win.viewport.apply_view("iso")
    pump(app, 0.6)
    try:
        pm = win.grab()
        pm.save(os.path.join(OUT, "07_janela_completa.png"))
        print("  07_janela_completa.png: OK")
    except Exception as exc:
        print(f"  win.grab falhou: {type(exc).__name__}: {exc}")

    print("\nResumo dos arquivos:")
    for f in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, f)
        print(f"  {f:36s} {os.path.getsize(p)/1024:8.1f} KB")
    print("\nRótulos/chaveamento exercitados:",
          win.viewport.render_mode, "| navegação livre:", win.viewport.walking)
    ok = win.viewport.toggle_walkthrough(True)
    print("Walkthrough ativado:", ok, "| câmera:", type(win.viewport.view.camera).__name__)
    win.viewport.toggle_walkthrough(False)
    print("Walkthrough desativado; câmera:",
          type(win.viewport.view.camera).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
