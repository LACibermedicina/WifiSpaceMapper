"""
Wi-Fi CSI Space Mapper -- API + servidor do cliente web.

    uvicorn server.app:app --host 0.0.0.0 --port 8080

Rotas principais
  /api/health                          estado do servidor e capacidades
  /api/auth/*                          registro, login, logout, perfil, idioma
  /api/projects/*                      projetos (uma captura = um projeto novo)
  /api/projects/{id}/captures/run      executa o pipeline CSI e salva a captura
  /api/projects/{id}/captures/upload   recebe nuvem/malha do cliente desktop
  /api/projects/{id}/measurements      reguas dinamicas (largura/altura/profundidade/distancia)
  /api/projects/{id}/vectors           vetores/polilinhas desenhados
  /api/projects/{id}/export            CSV / JSON / PDF / XLSX + OBJ / PLY / STL
  /api/users                           controle de usuarios (papel, ativo)
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from typing import List, Optional

import numpy as np
from fastapi import (Body, Depends, FastAPI, File, Form, HTTPException, Query,
                     Request, Response, UploadFile)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import auth, db, exporters as exp, pipeline_bridge as pb  # noqa: E402

COOKIE = "wsm_session"
WEB_DIR = os.path.join(ROOT, "web")
LANGS = ("pt", "en", "es", "ca")

app = FastAPI(title="Wi-Fi CSI Space Mapper", version="1.0.0",
              docs_url="/api/docs", openapi_url="/api/openapi.json")


# --------------------------------------------------------------------------
# autenticacao
# --------------------------------------------------------------------------
def current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE)
    if not token:
        hdr = request.headers.get("authorization") or ""
        if hdr.lower().startswith("bearer "):
            token = hdr[7:].strip()
    user = auth.user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="sessao_invalida")
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="requer_administrador")
    return user


def owned_project(pid: int, user: dict) -> dict:
    row = db.query_one("SELECT * FROM projects WHERE id=?", (pid,))
    if not row:
        raise HTTPException(404, "projeto_nao_encontrado")
    if row["user_id"] != user["id"] and user.get("role") != "admin":
        raise HTTPException(403, "sem_permissao")
    return row


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    auth.purge_sessions()
    os.makedirs(WEB_DIR, exist_ok=True)


# --------------------------------------------------------------------------
# saude / capacidades
# --------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"ok": True, "service": "wifi-csi-space-mapper", "version": "1.0.0",
            "time": db.now(), "langs": list(LANGS), "capabilities": pb.capabilities(),
            "presets": list(pb.PRESETS.keys()),
            "pdf": exp.PDF_OK, "xlsx": exp.XLSX_OK,
            "data_dir": db.DATA_DIR}


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
@app.post("/api/auth/register")
def register(request: Request, response: Response, payload: dict = Body(...)):
    email = (payload.get("email") or "").strip().lower()
    name = (payload.get("name") or "").strip()[:80]
    password = payload.get("password") or ""
    lang = payload.get("lang") if payload.get("lang") in LANGS else "pt"
    if "@" not in email or len(email) < 5:
        raise HTTPException(400, "email_invalido")
    err = auth.validate_password(password)
    if err:
        raise HTTPException(400, err)
    if db.query_one("SELECT id FROM users WHERE email=?", (email,)):
        raise HTTPException(409, "email_ja_registado")
    pwd_hash, salt = auth.hash_password(password)
    role = "admin" if not db.query_one("SELECT id FROM users LIMIT 1") else "user"
    uid = db.execute(
        "INSERT INTO users(email,name,pass_hash,salt,role,lang,active,created_at,last_login)"
        " VALUES(?,?,?,?,?,?,1,?,?)",
        (email, name or email.split("@")[0], pwd_hash, salt, role, lang,
         db.now(), db.now()))
    db.log_event(uid, None, "user_created", f"usuario {email} criado ({role})")
    token = auth.create_session(uid, request)
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax",
                        max_age=auth.SESSION_DAYS * 86400)
    user = auth.user_from_token(token)
    return {"user": user, "token": token}


@app.post("/api/auth/login")
def login(request: Request, response: Response, payload: dict = Body(...)):
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    row = db.query_one("SELECT * FROM users WHERE email=?", (email,))
    if not row or not auth.verify_password(password, row["salt"], row["pass_hash"]):
        raise HTTPException(401, "credenciais_invalidas")
    if not int(row["active"]):
        raise HTTPException(403, "usuario_desativado")
    db.execute("UPDATE users SET last_login=? WHERE id=?", (db.now(), row["id"]))
    token = auth.create_session(int(row["id"]), request)
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax",
                        max_age=auth.SESSION_DAYS * 86400)
    db.log_event(int(row["id"]), None, "login", f"login {email}")
    return {"user": auth.user_from_token(token), "token": token}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    auth.drop_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    return {"user": user}


@app.patch("/api/auth/me")
def update_me(payload: dict = Body(...), user: dict = Depends(current_user)):
    name = (payload.get("name") or user["name"])[:80]
    lang = payload.get("lang") if payload.get("lang") in LANGS else user["lang"]
    db.execute("UPDATE users SET name=?, lang=? WHERE id=?", (name, lang, user["id"]))
    if payload.get("password"):
        err = auth.validate_password(payload["password"])
        if err:
            raise HTTPException(400, err)
        h, s = auth.hash_password(payload["password"])
        db.execute("UPDATE users SET pass_hash=?, salt=? WHERE id=?", (h, s, user["id"]))
    row = db.query_one("SELECT id,email,name,role,lang,active,created_at,last_login"
                       " FROM users WHERE id=?", (user["id"],))
    return {"user": row}


# --------------------------------------------------------------------------
# usuarios (controle de acesso)
# --------------------------------------------------------------------------
@app.get("/api/users")
def list_users(user: dict = Depends(admin_user)):
    rows = db.query("SELECT id,email,name,role,lang,active,created_at,last_login"
                    " FROM users ORDER BY id")
    return {"users": rows, "count": len(rows)}


@app.patch("/api/users/{uid}")
def patch_user(uid: int, payload: dict = Body(...), user: dict = Depends(admin_user)):
    row = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not row:
        raise HTTPException(404, "usuario_nao_encontrado")
    role = payload.get("role", row["role"])
    active = int(payload.get("active", row["active"]))
    name = payload.get("name", row["name"])
    if uid == user["id"] and (role != "admin" or not active):
        raise HTTPException(400, "nao_pode_rebaixar_a_si_mesmo")
    db.execute("UPDATE users SET role=?, active=?, name=? WHERE id=?",
               (role, active, name, uid))
    db.log_event(user["id"], None, "user_update", f"usuario {uid}: role={role} ativo={active}")
    return {"ok": True}


@app.delete("/api/users/{uid}")
def delete_user(uid: int, user: dict = Depends(admin_user)):
    if uid == user["id"]:
        raise HTTPException(400, "nao_pode_excluir_a_si_mesmo")
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.log_event(user["id"], None, "user_delete", f"usuario {uid} excluido")
    return {"ok": True}


# --------------------------------------------------------------------------
# projetos
# --------------------------------------------------------------------------
def project_out(row: dict) -> dict:
    c = db.query_one("SELECT COUNT(*) n, MAX(created_at) last FROM captures WHERE project_id=?",
                     (row["id"],)) or {}
    m = db.query_one("SELECT COUNT(*) n FROM measurements WHERE project_id=?", (row["id"],)) or {}
    v = db.query_one("SELECT COUNT(*) n FROM vectors WHERE project_id=?", (row["id"],)) or {}
    d = dict(row)
    d["captures"] = int(c.get("n") or 0)
    d["measurements"] = int(m.get("n") or 0)
    d["vectors"] = int(v.get("n") or 0)
    d["last_capture"] = c.get("last")
    return d


@app.get("/api/projects")
def list_projects(user: dict = Depends(current_user),
                  q: str = Query("", max_length=80),
                  limit: int = Query(200, ge=1, le=1000)):
    if q:
        like = f"%{q}%"
        rows = db.query("SELECT * FROM projects WHERE user_id=? AND (name LIKE ? OR"
                        " description LIKE ? OR tags LIKE ?) ORDER BY updated_at DESC"
                        " LIMIT ?", (user["id"], like, like, like, limit))
    else:
        rows = db.query("SELECT * FROM projects WHERE user_id=? ORDER BY updated_at DESC"
                        " LIMIT ?", (user["id"], limit))
    return {"projects": [project_out(r) for r in rows], "count": len(rows)}


@app.post("/api/projects")
def create_project(payload: dict = Body(...), user: dict = Depends(current_user)):
    name = (payload.get("name") or "").strip()[:120] or f"Projeto {int(db.now())}"
    pid = db.execute(
        "INSERT INTO projects(user_id,name,description,tags,room_x,room_y,room_z,"
        "router_height,router_xyz,grid_step,voxel,source_mode,preset,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], name, (payload.get("description") or "")[:600],
         (payload.get("tags") or "")[:200],
         float(payload.get("room_x", 4.0)), float(payload.get("room_y", 5.0)),
         float(payload.get("room_z", 2.8)), float(payload.get("router_height", 1.4)),
         json.dumps(payload.get("router_xyz", [0, 0, 0])),
         float(payload.get("grid_step", 1.0)), float(payload.get("voxel", 0.12)),
         str(payload.get("source_mode", "sim")), str(payload.get("preset", "rapida")),
         db.now(), db.now()))
    db.log_event(user["id"], pid, "project_created", f"projeto '{name}' criado")
    return {"project": project_out(db.query_one("SELECT * FROM projects WHERE id=?", (pid,)))}


@app.get("/api/projects/{pid}")
def get_project(pid: int, user: dict = Depends(current_user)):
    row = owned_project(pid, user)
    return {"project": project_out(row)}


@app.patch("/api/projects/{pid}")
def patch_project(pid: int, payload: dict = Body(...), user: dict = Depends(current_user)):
    row = owned_project(pid, user)
    fields = {"name": str, "description": str, "tags": str, "room_x": float, "room_y": float,
              "room_z": float, "router_height": float, "grid_step": float, "voxel": float,
              "source_mode": str, "preset": str}
    sets, args = [], []
    for k, cast in fields.items():
        if k in payload and payload[k] is not None:
            sets.append(f"{k}=?")
            args.append(cast(payload[k]))
    if "router_xyz" in payload:
        sets.append("router_xyz=?")
        args.append(json.dumps(payload["router_xyz"]))
    if sets:
        sets.append("updated_at=?")
        args.append(db.now())
        args.append(pid)
        db.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", args)
        db.log_event(user["id"], pid, "project_updated", "projeto editado")
    return {"project": project_out(db.query_one("SELECT * FROM projects WHERE id=?", (pid,)))}


@app.delete("/api/projects/{pid}")
def delete_project(pid: int, user: dict = Depends(current_user)):
    owned_project(pid, user)
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    db.log_event(user["id"], None, "project_deleted", f"projeto {pid} excluido")
    return {"ok": True}


@app.get("/api/projects/{pid}/history")
def project_history(pid: int, user: dict = Depends(current_user)):
    owned_project(pid, user)
    events = db.query("SELECT * FROM events WHERE project_id=? ORDER BY ts DESC LIMIT 200",
                      (pid,))
    caps = db.query("SELECT id,label,kind,method,created_at,n_points,n_triangles,metrics"
                    " FROM captures WHERE project_id=? ORDER BY created_at DESC", (pid,))
    for c in caps:
        metrics = db.jloads(c.pop("metrics", "{}"), {})
        c["volume_m3"] = metrics.get("volume_voxel_m3", 0.0)
        c["coverage_25cm"] = metrics.get("coverage_25cm", metrics.get("cover_surface_25cm", 0.0))
        c["err_mean_m"] = metrics.get("err_cloud_mean_m", metrics.get("err_surface_mean_m", 0.0))
    return {"events": events, "captures": caps}


@app.get("/api/history")
def global_history(user: dict = Depends(current_user), limit: int = Query(60, ge=1, le=300)):
    rows = db.query(
        "SELECT p.id, p.name, p.updated_at, p.room_x, p.room_y, p.room_z,"
        " (SELECT COUNT(*) FROM captures c WHERE c.project_id=p.id) captures,"
        " (SELECT COUNT(*) FROM measurements m WHERE m.project_id=p.id) measurements,"
        " (SELECT COUNT(*) FROM vectors v WHERE v.project_id=p.id) vectors,"
        " (SELECT COALESCE(SUM(c.n_triangles),0) FROM captures c WHERE c.project_id=p.id) triangles"
        " FROM projects p WHERE p.user_id=? ORDER BY p.updated_at DESC LIMIT ?",
        (user["id"], limit))
    ev = db.query("SELECT * FROM events WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                  (user["id"], limit))
    return {"areas": rows, "events": ev}


# --------------------------------------------------------------------------
# capturas
# --------------------------------------------------------------------------
def _store_capture(user, proj, data: dict, params: dict, kind: str, label: str) -> int:
    pts = np.asarray(data["points"], dtype=np.float32).reshape(-1, 3)
    st = np.asarray(data.get("strengths", np.ones(len(pts))), dtype=np.float32).reshape(-1)
    if len(st) != len(pts):
        st = np.ones(len(pts), dtype=np.float32)
    pts4 = np.hstack([pts, st.reshape(-1, 1)]).astype(np.float32)
    mesh = data.get("mesh") or {}
    verts = np.asarray(mesh.get("vertices", np.zeros((0, 3))), dtype=np.float32).reshape(-1, 3)
    tris = np.asarray(mesh.get("triangles", np.zeros((0, 3))), dtype=np.int32).reshape(-1, 3)
    metrics = {k: float(v) for k, v in (data.get("metrics") or {}).items()}
    cid = db.execute(
        "INSERT INTO captures(project_id,user_id,label,kind,method,created_at,duration_s,"
        "n_points,n_triangles,voxel,metrics,calib,traj,points_blob,verts_blob,tris_blob)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (proj["id"], user["id"], label[:120], kind, str(data.get("method", ""))[:120],
         db.now(), float(metrics.get("duration_s", 0.0)), len(pts), len(tris),
         float(params.get("voxel", 0.12)), json.dumps(metrics),
         json.dumps(data.get("calib") or {}), json.dumps(db.as_list(data.get("traj"))),
         db.pack_array(pts4), db.pack_array(verts), db.pack_array(tris)))
    db.execute("UPDATE projects SET updated_at=?, source_mode=?, preset=?, voxel=?"
               " WHERE id=?", (db.now(), kind, str(params.get("preset", proj.get("preset"))),
                               float(params.get("voxel", proj.get("voxel", 0.12))), proj["id"]))
    db.log_event(user["id"], proj["id"], "capture_created",
                 f"captura #{cid} ({kind}) {len(pts)} pontos / {len(tris)} triangulos")
    return cid


def capture_out(row: dict, with_data: bool) -> dict:
    d = dict(row)
    d["metrics"] = db.jloads(d.pop("metrics", "{}"), {})
    d["calib"] = db.jloads(d.pop("calib", "{}"), {})
    traj = db.jloads(d.pop("traj", "[]"), [])
    if with_data:
        blob = d.pop("points_blob", None)
        verts_blob = d.pop("verts_blob", None)
        tris_blob = d.pop("tris_blob", None)
        pts4 = db.unpack_array(blob, np.float32)
        if pts4.size:
            pts4 = pts4.reshape(-1, 4)
            d["points"] = [[round(float(p[0]), 4), round(float(p[1]), 4),
                            round(float(p[2]), 4), round(float(p[3]), 4)] for p in pts4]
        else:
            d["points"] = []
        v = db.unpack_array(verts_blob, np.float32)
        t = db.unpack_array(tris_blob, np.int32)
        d["mesh"] = {
            "vertices": [[round(float(x), 4) for x in p] for p in v.reshape(-1, 3)],
            "triangles": [[int(a), int(b), int(c)] for a, b, c in t.reshape(-1, 3)]}
    else:
        d.pop("points_blob", None)
        d.pop("verts_blob", None)
        d.pop("tris_blob", None)
    d["traj"] = traj
    return d


@app.get("/api/projects/{pid}/captures")
def list_captures(pid: int, user: dict = Depends(current_user)):
    owned_project(pid, user)
    rows = db.query("SELECT * FROM captures WHERE project_id=? ORDER BY created_at DESC",
                    (pid,))
    return {"captures": [capture_out(r, with_data=False) for r in rows]}


@app.get("/api/captures/{cid}")
def get_capture(cid: int, user: dict = Depends(current_user)):
    row = db.query_one("SELECT * FROM captures WHERE id=?", (cid,))
    if not row:
        raise HTTPException(404, "captura_nao_encontrada")
    owned_project(int(row["project_id"]), user)
    return {"capture": capture_out(row, with_data=True)}


@app.post("/api/projects/{pid}/captures/run")
def run_capture(pid: int, payload: dict = Body(default={}),
                user: dict = Depends(current_user)):
    proj = owned_project(pid, user)
    params = {
        "room_x": float(payload.get("room_x", proj["room_x"])),
        "room_y": float(payload.get("room_y", proj["room_y"])),
        "room_z": float(payload.get("room_z", proj["room_z"])),
        "router_height": float(payload.get("router_height", proj["router_height"])),
        "voxel": float(payload.get("voxel", proj["voxel"])),
        "preset": str(payload.get("preset", proj["preset"] or "rapida")),
        "seed": int(payload.get("seed", 7)),
        "obstacles": payload.get("obstacles") or [],
    }
    try:
        data = pb.run_capture(params)
    except Exception as exc:  # pragma: no cover
        db.log_event(user["id"], pid, "capture_error", f"{type(exc).__name__}: {exc}")
        raise HTTPException(500, f"falha_no_pipeline: {type(exc).__name__}: {exc}")
    label = (payload.get("label") or "").strip()[:120] or \
        f"Captura {int(db.now())}"
    kind = str(payload.get("source_mode", "sim"))[:16]
    cid = _store_capture(user, proj, data, params, kind, label)
    return {"capture_id": cid, "summary": {k: v for k, v in data["metrics"].items()},
            "method": data["method"], "forces": data.get("forces"),
            "calib": data.get("calib"), "soft_error": data.get("soft_error"),
            "capabilities": data.get("capabilities"),
            "capture": capture_out(db.query_one("SELECT * FROM captures WHERE id=?", (cid,)),
                                   with_data=True)}


@app.post("/api/projects/{pid}/captures/upload")
def upload_capture(pid: int, payload: dict = Body(...),
                   user: dict = Depends(current_user)):
    """Recebe uma captura produzida por hardware (cliente desktop / ponte UDP)."""
    proj = owned_project(pid, user)
    raw_pts = payload.get("points") or []
    arr = np.asarray(raw_pts, dtype=np.float32)
    if arr.size == 0:
        pts = np.zeros((0, 4), dtype=np.float32)
    elif arr.ndim == 2 and arr.shape[1] == 4:
        pts = arr.reshape(-1, 4)
    else:
        arr = arr.reshape(-1, 3)
        st = np.asarray(payload.get("strengths") or np.ones(len(arr)), dtype=np.float32)
        if len(st) != len(arr):
            st = np.ones(len(arr), dtype=np.float32)
        pts = np.hstack([arr, st.reshape(-1, 1)])
    if len(pts) == 0:
        raise HTTPException(400, "nuvem_de_pontos_vazia")
    params = {"voxel": float(payload.get("voxel", proj["voxel"])),
              "preset": str(payload.get("preset", proj["preset"]))}
    data = {"points": pts[:, :3], "strengths": pts[:, 3],
            "mesh": payload.get("mesh") or None,
            "metrics": payload.get("metrics") or {}, "method": payload.get("method") or "upload",
            "calib": payload.get("calib") or {}, "traj": payload.get("traj") or []}
    if not data["mesh"]:
        from server import surface as _surf
        vox = _surf.voxel_surface(pts[:, :3], params["voxel"])
        if len(vox["vertices"]):
            data["mesh"] = {"vertices": vox["vertices"], "triangles": vox["triangles"]}
            data["metrics"]["volume_voxel_m3"] = vox["volume_m3"]
        data["method"] = f"{data['method']} + voxels"
    cid = _store_capture(user, proj, data, params, "hardware",
                         (payload.get("label") or "Captura de hardware"))
    return {"capture_id": cid,
            "capture": capture_out(db.query_one("SELECT * FROM captures WHERE id=?", (cid,)),
                                   with_data=True)}


@app.delete("/api/captures/{cid}")
def delete_capture(cid: int, user: dict = Depends(current_user)):
    row = db.query_one("SELECT * FROM captures WHERE id=?", (cid,))
    if not row:
        raise HTTPException(404, "captura_nao_encontrada")
    owned_project(int(row["project_id"]), user)
    db.execute("DELETE FROM captures WHERE id=?", (cid,))
    db.log_event(user["id"], int(row["project_id"]), "capture_deleted", f"captura {cid}")
    return {"ok": True}


@app.get("/api/captures/{cid}/download")
def download_capture(cid: int, fmt: str = Query("obj"),
                     user: dict = Depends(current_user)):
    row = db.query_one("SELECT * FROM captures WHERE id=?", (cid,))
    if not row:
        raise HTTPException(404, "captura_nao_encontrada")
    proj = owned_project(int(row["project_id"]), user)
    fmt = fmt.lower()
    pts4 = db.unpack_array(row["points_blob"], np.float32).reshape(-1, 4)
    verts = db.unpack_array(row["verts_blob"], np.float32).reshape(-1, 3)
    tris = db.unpack_array(row["tris_blob"], np.int32).reshape(-1, 3)
    base = exp.export_path(proj["name"], f"capture{cid}")
    meta = {"project": proj["name"], "capture_id": cid}
    if fmt in ("obj", "ply", "stl"):
        if len(tris) == 0:
            raise HTTPException(400, "malha_indisponivel")
        path, mime = exp.export_mesh(fmt, verts, tris, base, meta)
    elif fmt in ("csv", "json", "xyz"):
        path, mime = exp.export_cloud("xyz" if fmt == "xyz" else fmt,
                                      pts4[:, :3], pts4[:, 3], base,
                                      meta=meta)
    else:
        raise HTTPException(400, "formato_invalido")
    db.execute("INSERT INTO exports(user_id,project_id,fmt,scope,fields,filename,size,"
               "created_at) VALUES(?,?,?,?,?,?,?,?)",
               (user["id"], proj["id"], fmt, "cloud",
                json.dumps(["x", "y", "z", "distance_m", "strength"]),
                os.path.basename(path), os.path.getsize(path), db.now()))
    return FileResponse(path, media_type=mime, filename=os.path.basename(path))


# --------------------------------------------------------------------------
# medidas (reguas dinamicas) e vetores
# --------------------------------------------------------------------------
def _vec3(v, name="ponto"):
    try:
        a = np.asarray(v, dtype=float).reshape(3)
    except Exception:
        raise HTTPException(400, f"{name}_invalido")
    return a


def measure_payload(kind: str, p1, p2) -> dict:
    kind = (kind or "distance").lower()
    p1 = np.asarray(p1, dtype=float).reshape(3)
    p2 = np.asarray(p2, dtype=float).reshape(3)
    d = p2 - p1
    if kind == "width":
        d[1] = 0.0
        d[2] = 0.0
    elif kind == "depth":
        d[0] = 0.0
        d[2] = 0.0
    elif kind == "height":
        d[0] = 0.0
        d[1] = 0.0
    p2 = p1 + d
    return {"kind": kind, "p1": db.arr3(p1), "p2": db.arr3(p2),
            "value_m": float(np.linalg.norm(d)),
            "dx": float(d[0]), "dy": float(d[1]), "dz": float(d[2])}


@app.get("/api/projects/{pid}/measurements")
def list_measurements(pid: int, user: dict = Depends(current_user)):
    owned_project(pid, user)
    rows = db.query("SELECT * FROM measurements WHERE project_id=? ORDER BY created_at",
                    (pid,))
    return {"measurements": [exp.measurement_row(r) for r in rows]}


@app.post("/api/projects/{pid}/measurements")
def create_measurement(pid: int, payload: dict = Body(...),
                       user: dict = Depends(current_user)):
    owned_project(pid, user)
    m = measure_payload(payload.get("kind", "distance"), payload.get("p1", [0, 0, 0]),
                        payload.get("p2", [0, 0, 0]))
    mid = db.execute(
        "INSERT INTO measurements(project_id,capture_id,user_id,kind,label,p1,p2,value_m,"
        "dx,dy,dz,color,visible,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, payload.get("capture_id"), user["id"], m["kind"],
         (payload.get("label") or "")[:120], json.dumps(m["p1"]), json.dumps(m["p2"]),
         m["value_m"], m["dx"], m["dy"], m["dz"],
         (payload.get("color") or "#37a6e0")[:16], int(payload.get("visible", 1)),
         (payload.get("note") or "")[:400], db.now()))
    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (db.now(), pid))
    db.log_event(user["id"], pid, "measurement_created",
                 f"medida #{mid} {m['kind']} = {m['value_m']:.3f} m")
    row = db.query_one("SELECT * FROM measurements WHERE id=?", (mid,))
    return {"measurement": exp.measurement_row(row)}


@app.patch("/api/measurements/{mid}")
def patch_measurement(mid: int, payload: dict = Body(...),
                      user: dict = Depends(current_user)):
    row = db.query_one("SELECT * FROM measurements WHERE id=?", (mid,))
    if not row:
        raise HTTPException(404, "medida_nao_encontrada")
    owned_project(int(row["project_id"]), user)
    p1 = payload.get("p1", db.jloads(row["p1"], [0, 0, 0]))
    p2 = payload.get("p2", db.jloads(row["p2"], [0, 0, 0]))
    kind = payload.get("kind", row["kind"])
    m = measure_payload(kind, p1, p2)
    db.execute("UPDATE measurements SET kind=?,label=?,p1=?,p2=?,value_m=?,dx=?,dy=?,dz=?,"
               "color=?,visible=?,note=? WHERE id=?",
               (m["kind"], (payload.get("label", row["label"]) or "")[:120],
                json.dumps(m["p1"]), json.dumps(m["p2"]), m["value_m"], m["dx"], m["dy"],
                m["dz"], payload.get("color", row["color"]),
                int(payload.get("visible", row["visible"])),
                (payload.get("note", row["note"]) or "")[:400], mid))
    db.log_event(user["id"], int(row["project_id"]), "measurement_updated",
                 f"medida #{mid} -> {m['value_m']:.3f} m")
    return {"measurement": exp.measurement_row(
        db.query_one("SELECT * FROM measurements WHERE id=?", (mid,)))}


@app.delete("/api/measurements/{mid}")
def delete_measurement(mid: int, user: dict = Depends(current_user)):
    row = db.query_one("SELECT * FROM measurements WHERE id=?", (mid,))
    if not row:
        raise HTTPException(404, "medida_nao_encontrada")
    owned_project(int(row["project_id"]), user)
    db.execute("DELETE FROM measurements WHERE id=?", (mid,))
    db.log_event(user["id"], int(row["project_id"]), "measurement_deleted", f"medida {mid}")
    return {"ok": True}


@app.get("/api/projects/{pid}/vectors")
def list_vectors(pid: int, user: dict = Depends(current_user)):
    owned_project(pid, user)
    rows = db.query("SELECT * FROM vectors WHERE project_id=? ORDER BY created_at", (pid,))
    out = []
    for r in rows:
        d = exp.vector_derived(r)
        d.update({"id": r["id"], "name": r["name"], "note": r["note"],
                  "color": r["color"], "visible": bool(r["visible"]),
                  "closed": bool(r["closed"]), "capture_id": r["capture_id"],
                  "created_at": exp._iso(r["created_at"])})
        out.append(d)
    return {"vectors": out}


@app.post("/api/projects/{pid}/vectors")
def create_vector(pid: int, payload: dict = Body(...),
                  user: dict = Depends(current_user)):
    owned_project(pid, user)
    pts = np.asarray(payload.get("points") or [], dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 3 or len(pts) < 2:
        raise HTTPException(400, "vetor_precisa_de_2_pontos")
    row = {"points": json.dumps(db.arrN(pts)), "closed": int(bool(payload.get("closed")))}
    der = exp.vector_derived(row)
    vid = db.execute(
        "INSERT INTO vectors(project_id,capture_id,user_id,name,points,closed,total_m,"
        "perimeter_m,color,visible,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, payload.get("capture_id"), user["id"],
         (payload.get("name") or f"Vetor {int(db.now())}")[:120],
         json.dumps(db.arrN(pts)), int(bool(payload.get("closed"))),
         der["total_m"], der["perimeter_m"], (payload.get("color") or "#ffd166")[:16],
         int(payload.get("visible", 1)), (payload.get("note") or "")[:400], db.now()))
    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (db.now(), pid))
    db.log_event(user["id"], pid, "vector_created",
                 f"vetor #{vid} com {len(pts)} pontos, total {der['total_m']:.3f} m")
    r = db.query_one("SELECT * FROM vectors WHERE id=?", (vid,))
    d = exp.vector_derived(r)
    d.update({"id": vid, "name": r["name"], "note": r["note"], "color": r["color"],
              "visible": bool(r["visible"]), "closed": bool(r["closed"])})
    return {"vector": d}


@app.patch("/api/vectors/{vid}")
def patch_vector(vid: int, payload: dict = Body(...),
                 user: dict = Depends(current_user)):
    row = db.query_one("SELECT * FROM vectors WHERE id=?", (vid,))
    if not row:
        raise HTTPException(404, "vetor_nao_encontrado")
    owned_project(int(row["project_id"]), user)
    pts = payload.get("points", db.jloads(row["points"], []))
    closed = int(payload.get("closed", row["closed"]))
    der = exp.vector_derived({"points": json.dumps(pts), "closed": closed})
    db.execute("UPDATE vectors SET name=?,points=?,closed=?,total_m=?,perimeter_m=?,"
               "color=?,visible=?,note=? WHERE id=?",
               ((payload.get("name", row["name"]) or "")[:120], json.dumps(der["points"]),
                closed, der["total_m"], der["perimeter_m"],
                payload.get("color", row["color"]), int(payload.get("visible", row["visible"])),
                (payload.get("note", row["note"]) or "")[:400], vid))
    db.log_event(user["id"], int(row["project_id"]), "vector_updated",
                 f"vetor #{vid} -> {der['total_m']:.3f} m")
    r = db.query_one("SELECT * FROM vectors WHERE id=?", (vid,))
    d = exp.vector_derived(r)
    d.update({"id": vid, "name": r["name"], "note": r["note"], "color": r["color"],
              "visible": bool(r["visible"]), "closed": bool(r["closed"])})
    return {"vector": d}


@app.delete("/api/vectors/{vid}")
def delete_vector(vid: int, user: dict = Depends(current_user)):
    row = db.query_one("SELECT * FROM vectors WHERE id=?", (vid,))
    if not row:
        raise HTTPException(404, "vetor_nao_encontrado")
    owned_project(int(row["project_id"]), user)
    db.execute("DELETE FROM vectors WHERE id=?", (vid,))
    db.log_event(user["id"], int(row["project_id"]), "vector_deleted", f"vetor {vid}")
    return {"ok": True}


# --------------------------------------------------------------------------
# exportacao
# --------------------------------------------------------------------------
@app.get("/api/export/fields")
def export_fields(user: dict = Depends(current_user)):
    return exp.fields_registry()


@app.post("/api/projects/{pid}/export")
def export_project(pid: int, payload: dict = Body(...),
                   user: dict = Depends(current_user)):
    proj = owned_project(pid, user)
    fmt = str(payload.get("format", "csv")).lower()
    scope = str(payload.get("scope", "measurements")).lower()
    capture_id = payload.get("capture_id")
    cap = None
    if capture_id:
        cap = db.query_one("SELECT * FROM captures WHERE id=? AND project_id=?",
                           (int(capture_id), pid))
    if scope == "cloud":
        if not cap:
            raise HTTPException(400, "captura_necessaria_para_nuvem")
        pts4 = db.unpack_array(cap["points_blob"], np.float32).reshape(-1, 4)
        base = exp.export_path(proj["name"], "nuvem")
        path, mime = exp.export_cloud(fmt if fmt in ("csv", "json", "xyz") else "csv",
                                      pts4[:, :3], pts4[:, 3], base,
                                      fields=payload.get("fields") or None,
                                      meta={"project": proj["name"], "capture_id": int(capture_id)})
    elif scope == "mesh":
        if not cap:
            raise HTTPException(400, "captura_necessaria_para_malha")
        verts = db.unpack_array(cap["verts_blob"], np.float32).reshape(-1, 3)
        tris = db.unpack_array(cap["tris_blob"], np.int32).reshape(-1, 3)
        if len(tris) == 0:
            raise HTTPException(400, "malha_indisponivel")
        base = exp.export_path(proj["name"], "malha")
        path, mime = exp.export_mesh(fmt if fmt in ("obj", "ply", "stl") else "obj",
                                     verts, tris, base, {"project": proj["name"]})
    else:
        mrows = [exp.measurement_row(r) for r in db.query(
            "SELECT * FROM measurements WHERE project_id=? ORDER BY created_at", (pid,))]
        vrows = []
        for r in db.query("SELECT * FROM vectors WHERE project_id=? ORDER BY created_at", (pid,)):
            d = exp.vector_derived(r)
            d.update({"id": r["id"], "name": r["name"], "note": r["note"],
                      "created_at": exp._iso(r["created_at"]),
                      "closed": bool(r["closed"])})
            vrows.append(d)
        if scope == "measurements":
            rows, fields = mrows, payload.get("fields") or list(exp.MEASUREMENT_FIELDS)[:8]
            rows = [{"id": r["id"], "kind": r["kind"], "label": r["label"],
                     "value_m": r["value_m"], "value_cm": r["value_cm"],
                     "dx": r["dx"], "dy": r["dy"], "dz": r["dz"],
                     "p1": r["p1"], "p2": r["p2"], "capture_id": r["capture_id"],
                     "project_id": r["project_id"], "note": r["note"],
                     "created_at": r["created_at"], "visible": r["visible"]} for r in rows]
            label = "Medidas e distancias"
        elif scope == "vectors":
            rows, fields = vrows, payload.get("fields") or list(exp.VECTOR_FIELDS)[:9]
            label = "Vetores e perimetros"
        else:  # capture_summary
            caps = db.query("SELECT * FROM captures WHERE project_id=? ORDER BY created_at",
                            (pid,))
            rows = []
            for c in caps:
                mt = db.jloads(c["metrics"], {})
                rows.append({"id": c["id"], "label": c["label"], "kind": c["kind"],
                             "method": c["method"], "created_at": exp._iso(c["created_at"]),
                             "n_points": c["n_points"], "n_triangles": c["n_triangles"],
                             "volume_m3": round(float(mt.get("volume_voxel_m3", 0.0)), 4),
                             "coverage_25cm": round(float(mt.get("coverage_25cm",
                                                                mt.get("cover_surface_25cm", 0.0))), 4),
                             "err_mean_m": round(float(mt.get("err_cloud_mean_m",
                                                             mt.get("err_surface_mean_m", 0.0))), 4),
                             "duration_s": c["duration_s"]})
            fields = payload.get("fields") or ["id", "label", "kind", "method", "n_points",
                                               "n_triangles", "volume_m3", "coverage_25cm",
                                               "err_mean_m", "duration_s", "created_at"]
            label = "Resumo das capturas"
        if not rows:
            raise HTTPException(400, "nada_para_exportar")
        base = exp.export_path(proj["name"], scope)
        meta = {"title": f"{proj['name']} -- {label}",
                "scope_label": label,
                "info": {"Projeto": proj["name"], "Descricao": proj["description"] or "-",
                         "Comodo": f"{proj['room_x']:.2f} x {proj['room_y']:.2f} x {proj['room_z']:.2f} m",
                         "Altura do roteador": f"{proj['router_height']:.2f} m",
                         "Registros": len(rows),
                         "Usuario": user["name"],
                         "Emitido": exp._iso(db.now())}}
        path, mime = exp.export_tabular(fmt, scope, fields, rows, meta, base)
    eid = db.execute("INSERT INTO exports(user_id,project_id,fmt,scope,fields,filename,size,"
                     "created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (user["id"], pid, fmt, scope, json.dumps(payload.get("fields") or []),
                      os.path.basename(path), os.path.getsize(path), db.now()))
    db.log_event(user["id"], pid, "export", f"{scope}.{fmt} -> {os.path.basename(path)}")
    return {"export_id": eid, "filename": os.path.basename(path),
            "url": f"/api/exports/{eid}/download", "size": os.path.getsize(path),
            "rows": None}


@app.get("/api/exports/{eid}/download")
def download_export(eid: int, user: dict = Depends(current_user)):
    row = db.query_one("SELECT * FROM exports WHERE id=?", (eid,))
    if not row:
        raise HTTPException(404, "exportacao_nao_encontrada")
    if row["user_id"] != user["id"] and user.get("role") != "admin":
        raise HTTPException(403, "sem_permissao")
    path = os.path.join(db.EXPORT_DIR, os.path.basename(row["filename"]))
    if not os.path.exists(path):
        raise HTTPException(404, "arquivo_removido")
    return FileResponse(path, filename=os.path.basename(path))


@app.get("/api/projects/{pid}/exports")
def list_exports(pid: int, user: dict = Depends(current_user)):
    owned_project(pid, user)
    rows = db.query("SELECT id,fmt,scope,filename,size,created_at FROM exports"
                    " WHERE project_id=? ORDER BY created_at DESC LIMIT 100", (pid,))
    for r in rows:
        r["url"] = f"/api/exports/{r['id']}/download"
        r["created_at_iso"] = exp._iso(r["created_at"])
    return {"exports": rows}


# --------------------------------------------------------------------------
# cliente web estatico (por ultimo, para nao sombrear /api)
# --------------------------------------------------------------------------
if os.path.isdir(WEB_DIR):
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
