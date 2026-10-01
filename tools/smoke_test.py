"""Verificacao de ponta a ponta do portal (executa contra um servidor real).

    python tools/smoke_test.py http://127.0.0.1:8091

Faz: registro -> projeto -> captura CSI -> medidas -> vetor -> exportacoes ->
recarga dos dados vinculados ao projeto -> download da malha.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8091").rstrip("/")
OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  [ok]  " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


def http(method, path, payload=None, token=None, raw=False):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=900) as r:
        body = r.read()
        return body if raw else (json.loads(body.decode()) if body else {})


def main():
    print(f"== portal em {BASE} ==")
    h = http("GET", "/api/health")
    check("health responde", h.get("ok") is True)
    check("pipeline csi_mapper carregado", h["capabilities"]["csi_pipeline"] is True)
    check("quatro idiomas", h.get("langs") == ["pt", "en", "es", "ca"])
    check("exportadores pdf/xlsx", h.get("pdf") and h.get("xlsx"))
    print("     open3d:", h["capabilities"]["open3d"], "| modo:", h["capabilities"]["mode"])

    email = f"teste_{uuid.uuid4().hex[:8]}@dominio.com"
    reg = http("POST", "/api/auth/register",
               {"email": email, "name": "Teste Automatizado", "password": "SenhaForte123", "lang": "pt"})
    token = reg["token"]
    check("registro de usuario", reg["user"]["email"] == email)
    check("papel valido atribuido", reg["user"]["role"] in ("admin", "user"))
    is_admin = reg["user"]["role"] == "admin"
    try:
        users_all = http("GET", "/api/users", token=token)["users"]
        check("controle de usuarios responde ao administrador", len(users_all) > 0,
              f"{len(users_all)} contas")
        oldest = min(users_all, key=lambda u: u["id"])
        check("primeira conta do portal e administradora", oldest["role"] == "admin",
              f"id={oldest['id']}")
    except urllib.error.HTTPError as e:
        check("usuario comum e bloqueado em /api/users (403)",
              e.code == 403 and not is_admin, f"http {e.code}")

    try:
        http("POST", "/api/auth/login", {"email": email, "password": "errada"})
        check("login com senha errada e recusado", False)
    except urllib.error.HTTPError as e:
        check("login com senha errada e recusado", e.code == 401)
    check("login correto", http("POST", "/api/auth/login",
                                {"email": email, "password": "SenhaForte123"})["user"]["email"] == email)
    check("sessao /me", http("GET", "/api/auth/me", token=token)["user"]["email"] == email)

    proj = http("POST", "/api/projects",
                {"name": "Sala de testes", "description": "smoke", "tags": "teste",
                 "room_x": 4.5, "room_y": 5.5, "room_z": 2.8, "preset": "rapida"}, token)
    pid = proj["project"]["id"]
    check("projeto criado (um projeto por area)", pid > 0)

    cap = http("POST", f"/api/projects/{pid}/captures/run",
               {"label": "captura smoke", "room_x": 4.5, "room_y": 5.5, "room_z": 2.8,
                "preset": "rapida", "voxel": 0.16}, token)
    cid = cap["capture_id"]
    m = cap["summary"]
    check("captura CSI executada", cid > 0)
    check("nuvem de reflexoes gerada", m.get("n_points", 0) > 30, f"pontos={int(m.get('n_points', 0))}")
    check("malha 3D reconstruida", m.get("n_triangles", 0) > 100,
          f"triangulos={int(m.get('n_triangles', 0))} metodo={cap['method']}")
    check("metrica de cobertura presente", "coverage_25cm" in m or "cover_surface_25cm" in m)
    check("calibracao registrada", bool(cap.get("calib")))

    meas_specs = [("width", [0.0, 0.0, -1.4], [4.5, 0.0, -1.4]),
                  ("depth", [0.0, 0.0, -1.4], [0.0, 5.5, -1.4]),
                  ("height", [0.0, 0.0, -1.4], [0.0, 0.0, 1.4]),
                  ("distance", [1.0, 0.0, 0.0], [0.0, 1.0, 0.0])]
    ids = []
    for kind, p1, p2 in meas_specs:
        r = http("POST", f"/api/projects/{pid}/measurements",
                 {"kind": kind, "p1": p1, "p2": p2, "capture_id": cid,
                  "label": kind + " smoke"}, token)
        ids.append(r["measurement"]["id"])
    check("quatro medidas criadas (largura/profundidade/altura/distancia)", len(ids) == 4)

    lst = http("GET", f"/api/projects/{pid}/measurements", token=token)["measurements"]
    check("medidas recarregam vinculadas ao projeto", len(lst) == 4, f"n={len(lst)}")
    by_kind = {x["kind"]: x for x in lst}
    check("largura mede apenas X", abs(by_kind["width"]["value_m"] - 4.5) < 1e-6
          and abs(by_kind["width"]["dy"]) < 1e-9 and abs(by_kind["width"]["dz"]) < 1e-9,
          f"={by_kind['width']['value_m']:.3f} m")
    check("profundidade mede apenas Y", abs(by_kind["depth"]["value_m"] - 5.5) < 1e-6,
          f"={by_kind['depth']['value_m']:.3f} m")
    check("altura mede apenas Z", abs(by_kind["height"]["value_m"] - 2.8) < 1e-6,
          f"={by_kind['height']['value_m']:.3f} m")
    check("distancia 3D livre", abs(by_kind["distance"]["value_m"] - 2 ** 0.5) < 2e-3,
          f"={by_kind['distance']['value_m']:.4f} m (esperado 1.4142)")
    check("valor em cm coerente", abs(by_kind["height"]["value_cm"] - 280.0) < 1e-3)

    forged = http("PATCH", f"/api/measurements/{ids[0]}", {"value_m": 999}, token)["measurement"]
    check("valor da medida e derivado no servidor (nao aceita valor inventado pelo cliente)",
          abs(forged["value_m"] - 4.5) < 1e-6, f"={forged['value_m']:.3f} m")
    edited = http("PATCH", f"/api/measurements/{ids[0]}",
                  {"p2": [6.0, 0.0, -1.4], "label": "largura corrigida"}, token)["measurement"]
    check("medida editada recalcula", abs(edited["value_m"] - 6.0) < 1e-6,
          f"={edited['value_m']:.3f} m")

    vec = http("POST", f"/api/projects/{pid}/vectors",
               {"name": "contorno da sala", "closed": True, "capture_id": cid,
                "points": [[-2.25, -2.75, -1.4], [2.25, -2.75, -1.4],
                           [2.25, 2.75, -1.4], [-2.25, 2.75, -1.4]]}, token)["vector"]
    check("vetor desenhado guardado", vec["n_points"] == 4)
    check("comprimento do vetor = soma dos 3 segmentos tracados",
          abs(vec["total_m"] - (4.5 + 5.5 + 4.5)) < 1e-3, f"={vec['total_m']:.2f} m")
    check("segmento de fecho entra so no perimetro",
          abs(vec["total_m"] - 14.5) < 1e-3 and abs(vec["perimeter_m"] - 20.0) < 1e-3,
          f"aberto={vec['total_m']:.2f} m fechado={vec['perimeter_m']:.2f} m")
    check("perimetro com fecho", abs(vec["perimeter_m"] - (4.5 + 5.5) * 2) < 1e-3)
    check("area projetada no piso", abs(vec["area_m2"] - 4.5 * 5.5) < 1e-2,
          f"={vec['area_m2']:.2f} m2")

    vecs = http("GET", f"/api/projects/{pid}/vectors", token=token)["vectors"]
    check("vetor recarrega vinculado ao projeto", len(vecs) == 1 and len(vecs[0]["segments"]) == 4)

    fields = http("GET", "/api/export/fields", token=token)
    check("registro de campos para exportacao", "measurements" in fields and "vectors" in fields)

    outs = {}
    for scope, fmt, flds in [
            ("measurements", "csv", ["kind", "value_m", "value_cm", "dx", "dy", "dz", "p1", "p2"]),
            ("measurements", "json", ["kind", "value_m", "value_cm"]),
            ("measurements", "pdf", ["kind", "label", "value_m", "value_cm", "p1", "p2"]),
            ("measurements", "xlsx", ["kind", "value_m", "value_cm"]),
            ("vectors", "csv", ["name", "n_points", "total_m", "perimeter_m", "area_m2"]),
            ("vectors", "pdf", ["name", "total_m", "perimeter_m", "area_m2", "segments"]),
            ("capture_summary", "csv", ["id", "label", "n_points", "n_triangles", "volume_m3"])]:
        r = http("POST", f"/api/projects/{pid}/export",
                 {"scope": scope, "format": fmt, "fields": flds, "capture_id": cid}, token)
        blob = http("GET", r["url"], token=token, raw=True)
        outs[f"{scope}.{fmt}"] = (r["filename"], r["size"], blob)
        check(f"export {scope}.{fmt}", len(blob) > 60, f"{r['filename']} {r['size']} B")

    csv_txt = outs["measurements.csv"][2].decode("utf-8-sig")
    check("CSV contem os 4 tipos de medida",
          all(k in csv_txt for k in ("width", "depth", "height", "distance")))
    check("CSV contem cabecalho escolhido", csv_txt.splitlines()[0].startswith("kind,value_m,value_cm"))
    check("PDF valido", outs["measurements.pdf"][2].startswith(b"%PDF"))
    check("XLSX valido (zip)", outs["measurements.xlsx"][2].startswith(b"PK"))

    for scope, fmt in [("cloud", "csv"), ("cloud", "json"), ("cloud", "xyz")]:
        r = http("POST", f"/api/projects/{pid}/export",
                 {"scope": scope, "format": fmt, "capture_id": cid, "fields": []}, token)
        blob = http("GET", r["url"], token=token, raw=True)
        check(f"export {scope}.{fmt}", len(blob) > 500, f"{r['size']} B")
    for fmt in ("obj", "ply", "stl"):
        r = http("POST", f"/api/projects/{pid}/export",
                 {"scope": "mesh", "format": fmt, "capture_id": cid, "fields": []}, token)
        blob = http("GET", r["url"], token=token, raw=True)
        check(f"export malha .{fmt}", len(blob) > 500, f"{r['size']} B")
    obj = http("GET", f"/api/captures/{cid}/download?fmt=obj", token=token, raw=True).decode("utf-8", "ignore")
    check("malha OBJ abre com vertices e faces", obj.count("\nv ") > 100 and obj.count("\nf ") > 100,
          f"v={obj.count(chr(10)+'v ')}, f={obj.count(chr(10)+'f ')}")

    hist = http("GET", f"/api/projects/{pid}/history", token=token)
    check("historico do projeto registra a captura",
          any(c["id"] == cid for c in hist["captures"]))
    glob = http("GET", "/api/history", token=token)
    area = [a for a in glob["areas"] if a["id"] == pid]
    check("historico de areas conta capturas/medidas/vetores",
          bool(area) and area[0]["captures"] == 1 and area[0]["measurements"] == 4
          and area[0]["vectors"] == 1,
          f"caps={area[0]['captures']} med={area[0]['measurements']} vec={area[0]['vectors']}")

    if is_admin:
        users = http("GET", "/api/users", token=token)["users"]
        check("controle de usuarios lista contas", any(u["email"] == email for u in users))
        uid = [u for u in users if u["email"] == email][0]["id"]
        check("administrador altera papel de usuario",
              http("PATCH", f"/api/users/{uid}", {"role": "admin"}, token).get("ok") is True)
        check("administrador ativa/desativa usuario",
              http("PATCH", f"/api/users/{uid}", {"active": 1}, token).get("ok") is True)
    else:
        check("controle de usuarios exige administrador", True, "conta comum (esperado)")

    other = http("POST", "/api/auth/register",
                 {"email": f"outro_{uuid.uuid4().hex[:6]}@dominio.com", "password": "SenhaForte123"})
    try:
        http("GET", f"/api/projects/{pid}/measurements", token=other["token"])
        check("isolamento entre usuarios", False)
    except urllib.error.HTTPError as e:
        check("isolamento entre usuarios (403)", e.code == 403)

    breadcrumb = http("GET", f"/api/projects/{pid}", token=token)["project"]
    check("projeto consolida contadores",
          breadcrumb["captures"] == 1 and breadcrumb["measurements"] == 4 and breadcrumb["vectors"] == 1)

    print(f"\n== resultado: {len(OK)} ok, {len(FAIL)} falhas ==")
    if FAIL:
        print("falhas:", ", ".join(FAIL))
        return 1
    print(f"projeto de teste #{pid}, captura #{cid}, e-mail {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
