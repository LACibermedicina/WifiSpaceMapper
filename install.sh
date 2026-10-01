#!/usr/bin/env bash
# ==========================================================================
#  Wi-Fi CSI Space Mapper -- instalador do servidor web
#  Instala dependencias, cria o ambiente virtual, inicializa o banco,
#  (opcionalmente) registra o servico systemd e mostra como publicar.
#
#  Uso tipico:
#     chmod +x install.sh
#     ./install.sh                      # porta 8080, instala Open3D
#     ./install.sh --port 9000
#     ./install.sh --no-open3d          # maquinas sem GL / instalacao minima
#     sudo ./install.sh --systemd       # cria o servico e inicia
#     ./install.sh --admin admin@dominio.com --admin-pass 'SenhaForte123'
# ==========================================================================
set -euo pipefail

APP_NAME="wifi-space-mapper-web"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
WITH_OPEN3D=1
DO_SYSTEMD=0
ADMIN_EMAIL=""
ADMIN_PASS=""
SERVICE_NAME="wifi-space-mapper"

c_reset="\033[0m"; c_ok="\033[1;32m"; c_warn="\033[1;33m"; c_err="\033[1;31m"; c_info="\033[1;36m"
log()  { printf "${c_info}[info]${c_reset} %s\n" "$*"; }
ok()   { printf "${c_ok}[ ok ]${c_reset} %s\n" "$*"; }
warn() { printf "${c_warn}[aviso]${c_reset} %s\n" "$*"; }
die()  { printf "${c_err}[erro]${c_reset} %s\n" "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --no-open3d) WITH_OPEN3D=0; shift ;;
    --systemd) DO_SYSTEMD=1; shift ;;
    --admin) ADMIN_EMAIL="$2"; shift 2 ;;
    --admin-pass) ADMIN_PASS="$2"; shift 2 ;;
    --venv) VENV_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "opcao desconhecida: $1" ;;
  esac
done

log "diretorio da aplicacao: $ROOT_DIR"

# ------------------------------------------------------------------ 1) Python
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "python3 nao encontrado no PATH"
PY_VER="$($PYTHON_BIN -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
log "python detectado: $PYTHON_BIN ($PY_VER)"
"$PYTHON_BIN" - <<'PY' || die "e necessario Python 3.9 ou superior"
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY

# ------------------------------------------------------- 2) dependencias de SO
install_system_deps() {
  if command -v apt-get >/dev/null 2>&1; then
    if [[ "$(id -u)" -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi
    if $SUDO -n true 2>/dev/null || [[ "$(id -u)" -eq 0 ]]; then
      log "instalando pacotes de sistema (python3-venv, libGL, xvfb, curl)..."
      $SUDO apt-get update -qq || warn "apt-get update falhou (seguindo)"
      # libEGL/libGL sao exigidos pelo Open3D; libglib/xvfb evitam falhas headless
      $SUDO apt-get install -y -qq python3-venv python3-dev build-essential \
        libgl1 libegl1 libgles2 libglvnd0 libglx0 libglib2.0-0 libgomp1 \
        libx11-6 libxext6 libsm6 curl ca-certificates unzip xvfb >/dev/null 2>&1 \
        || warn "alguns pacotes de sistema nao puderam ser instalados (seguindo)"
      ok "pacotes de sistema verificados"
    else
      warn "sem sudo: pulei a instalacao de pacotes de sistema"
    fi
  else
    warn "apt-get nao disponivel: instale manualmente python3-venv e libGL"
  fi
}
install_system_deps

# ---------------------------------------------------------------- 3) venv+pip
if [[ ! -d "$VENV_DIR" ]]; then
  log "criando ambiente virtual em $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR" || die "falha ao criar o venv (python3-venv instalado?)"
fi
PY="$VENV_DIR/bin/python"
[[ -x "$PY" ]] || die "interpretador do venv nao encontrado em $PY"

log "atualizando pip/setuptools/wheel"
"$PY" -m pip install --quiet --upgrade pip setuptools wheel

log "instalando dependencias do portal (fastapi, uvicorn, numpy, scipy, sklearn, reportlab)"
"$PY" -m pip install --quiet -r "$ROOT_DIR/requirements-web.txt" \
  || die "falha ao instalar requirements-web.txt"

if [[ "$WITH_OPEN3D" -eq 1 ]]; then
  log "instalando Open3D (malha suave Poisson/Alpha Shapes) -- pode demorar alguns minutos"
  if "$PY" -m pip install --quiet "open3d>=0.17"; then
    ok "Open3D instalado"
  else
    warn "Open3D nao pode ser instalado nesta maquina; o portal usara a malha por voxels"
  fi
else
  warn "--no-open3d: malha sera gerada por voxels (sem Poisson)"
fi

# ------------------------------------------------------------------- 4) dados
mkdir -p "$ROOT_DIR/data/exports" "$ROOT_DIR/logs"
chmod +x "$ROOT_DIR/run.sh" 2>/dev/null || true

# --------------------------------------------------------------- 5) segredo
ENV_FILE="$ROOT_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  SECRET="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(48))')"
  cat > "$ENV_FILE" <<EOF
# Gerado por install.sh em $(date -Iseconds)
WSM_HOST=$HOST
WSM_PORT=$PORT
WSM_SECRET=$SECRET
WSM_DATA=$ROOT_DIR/data
# Compatibilidade com o software desktop (GUI)
QT_QPA_PLATFORM=
EOF
  chmod 600 "$ENV_FILE"
  ok "arquivo .env criado"
else
  log ".env ja existia (mantido)"
fi

# ------------------------------------------------------------------ 6) banco
log "inicializando o banco SQLite e o pacote de dados"
"$PY" - <<PY
import sys
sys.path.insert(0, r"$ROOT_DIR")
from server import db
db.init_db()
print("banco:", db.DB_PATH)
PY
ok "banco inicializado"

# ------------------------------------------------------- 7) usuario inicial
if [[ -n "$ADMIN_EMAIL" && -n "$ADMIN_PASS" ]]; then
  log "criando usuario administrador $ADMIN_EMAIL"
  "$PY" - <<PY
import sys
sys.path.insert(0, r"$ROOT_DIR")
from server import db, auth
email = r"$ADMIN_EMAIL".strip().lower()
existing = db.query_one("SELECT id FROM users WHERE email=?", (email,))
if existing:
    print("usuario ja existe:", email)
else:
    h, s = auth.hash_password(r"$ADMIN_PASS")
    first = not db.query_one("SELECT id FROM users LIMIT 1")
    db.execute("INSERT INTO users(email,name,pass_hash,salt,role,lang,active,created_at)"
               " VALUES(?,?,?,?,?,?,1,?)",
               (email, email.split("@")[0], h, s, "admin" if first else "user", "pt", db.now()))
    print("usuario criado:", email, "(admin)" if first else "(usuario)")
PY
  ok "usuario administrador pronto"
else
  log "nenhum admin informado: o PRIMEIRO usuario registrado na interface vira administrador"
fi

# ------------------------------------------------------------- 8) systemd
if [[ "$DO_SYSTEMD" -eq 1 ]]; then
  [[ "$(id -u)" -eq 0 ]] || die "--systemd exige privilegios de root (use sudo)"
  RUN_USER="${SUDO_USER:-$(stat -c '%U' "$ROOT_DIR")}"
  UNIT="/etc/systemd/system/$SERVICE_NAME.service"
  log "instalando servico systemd em $UNIT (usuario $RUN_USER)"
  sed -e "s|__ROOT__|$ROOT_DIR|g" -e "s|__USER__|$RUN_USER|g" \
      -e "s|__PORT__|$PORT|g" "$ROOT_DIR/deploy/wifi-space-mapper.service" > "$UNIT"
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null
  systemctl restart "$SERVICE_NAME"
  sleep 2
  systemctl --no-pager --lines=6 status "$SERVICE_NAME" || true
  ok "servico $SERVICE_NAME ativo (systemctl status $SERVICE_NAME)"
fi

# ------------------------------------------------------------ 9) verificacao
log "verificando o servidor localmente (porta $PORT)"
if [[ "$DO_SYSTEMD" -eq 0 ]]; then
  pkill -f "uvicorn server.app:app --host 127.0.0.1 --port $PORT" 2>/dev/null || true
  sleep 1
  ( cd "$ROOT_DIR" && nohup "$VENV_DIR/bin/uvicorn" server.app:app --host 127.0.0.1 \
      --port "$PORT" > "$ROOT_DIR/logs/install_check.log" 2>&1 & echo $! > /tmp/wsm_check.pid )
  for _ in $(seq 1 30); do
    sleep 1
    if curl -fsS "http://127.0.0.1:$PORT/api/health" >/tmp/wsm_health.json 2>/dev/null; then break; fi
  done
  if [[ -s /tmp/wsm_health.json ]]; then
    ok "servidor respondeu: $(cat /tmp/wsm_health.json | head -c 300)"
  else
    warn "nao foi possivel validar via HTTP agora (veja logs/install_check.log)"
  fi
  [[ -f /tmp/wsm_check.pid ]] && kill "$(cat /tmp/wsm_check.pid)" 2>/dev/null || true
  rm -f /tmp/wsm_check.pid
fi

IP_GUESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

====================================================================
  Instalacao concluida.
====================================================================
  Iniciar (primeiro plano):     ./run.sh
  Iniciar (segundo plano):      ./run.sh --daemon
  Parar:                        ./run.sh --stop
  Porta:                        $PORT

  Acesso no proprio servidor:   http://127.0.0.1:$PORT
  Acesso do cliente (LAN):      http://${IP_GUESS:-SEU_IP}:${PORT}

  Primeiro acesso: crie a conta inicial (ela se torna administradora).
  Documentacao completa: MANUAL_INSTALACAO.md
====================================================================
EOF
