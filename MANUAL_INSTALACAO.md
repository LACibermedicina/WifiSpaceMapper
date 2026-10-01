# Manual de instalacao e uso — Wi-Fi CSI Space Mapper

Portal web para mapeamento 3D de ambientes por **CSI Wi-Fi**, com controle de
usuarios, historico de areas mapeadas, reguas dinamicas para extrair medidas e
exportacao de dados conforme a necessidade. Interface traduzida em
**portugues, ingles, espanhol e catalao**.

---

## 1. Visao geral

O sistema tem duas partes:

| Parte | Onde roda | O que faz |
|---|---|---|
| **Servidor** (esta pasta) | seu servidor remoto (Linux) | executa o pipeline CSI, guarda usuarios/projetos/capturas/medidas, gera exportacoes |
| **Cliente** | o navegador de quem vai mapear | interface 3D interativa e animada, reguas dinamicas, historico, exportacao |

O cliente **nao instala nada**: abre a URL do servidor no navegador (Chrome,
Edge, Firefox ou Safari) e trabalha. Ele usa WebGL, portanto nao ha download de
plugins nem app.

O motor de mapeamento e o mesmo software Python que voce ja tinha
(`csi_mapper`): sintese/leitura de CSI, calibracao, **MUSIC 2D** de azimute x
elevacao, **MUSIC 1D** de ToF, geometria raio-elipsoide, agrupamento **DBSCAN**
e reconstrucao **Poisson / Alpha Shapes** com Open3D.

---

## 2. Requisitos

**Servidor**
* Linux (Ubuntu 22.04/24.04, Debian 12, RHEL/Rocky 9 ou similar)
* Python **3.9+** (recomendado 3.11)
* 2 nucleos, 4 GB de RAM e 5 GB de disco minimo (recomendado 4 nucleos / 8 GB)
* Porta TCP livre (padrao **8080**) — ou 80/443 com Nginx na frente
* Acesso `sudo` para instalar pacotes de sistema e (opcional) o servico systemd

**Cliente**
* Navegador moderno com WebGL 2 ativo
* Placa grafica com aceleracao de hardware (qualquer notebook dos ultimos
  10 anos serve)

---

## 3. Instalacao em 5 minutos

```bash
# 1) envie/copie a pasta do projeto para o servidor
scp wifi-space-mapper-web.zip usuario@servidor:/opt/

# 2) no servidor
ssh usuario@servidor
cd /opt && unzip wifi-space-mapper-web.zip && cd wifi-space-mapper-web
chmod +x install.sh run.sh

# 3) instalar (cria venv, instala dependencias, inicializa o banco)
./install.sh

# 4) iniciar
./run.sh --daemon
```

Pronto. Acesse `http://IP-DO-SERVIDOR:8080` de qualquer cliente da rede e crie
a conta inicial — **o primeiro usuario registrado vira administrador**.

### Opcoes uteis do instalador

```bash
./install.sh --port 9000                # muda a porta
./install.sh --no-open3d                # instalacao minima (malha por voxels)
./install.sh --admin eu@dominio.com --admin-pass 'SenhaForte123'
sudo ./install.sh --systemd             # cria e inicia o servico no boot
```

| Opcao | Para que serve |
|---|---|
| `--port N` | porta de escuta (padrao 8080) |
| `--host H` | interface de escuta (padrao 0.0.0.0) |
| `--no-open3d` | pula o Open3D (maquinas sem GL/sem espaco; malha sai por voxels) |
| `--systemd` | instala a unit `wifi-space-mapper.service` e sobe no boot |
| `--admin EMAIL --admin-pass SENHA` | cria o administrador sem usar a interface |
| `--venv CAMINHO` | escolhe onde fica o ambiente virtual |

### Comandos do dia a dia

```bash
./run.sh            # primeiro plano (Ctrl+C encerra)
./run.sh --daemon   # segundo plano, logs em logs/server.log
./run.sh --stop     # encerra o servico em segundo plano
sudo systemctl status wifi-space-mapper     # se instalado com --systemd
sudo journalctl -u wifi-space-mapper -f     # logs do systemd
```

---

## 4. Publicar com HTTPS (recomendado)

O instalador ja deixa um exemplo de proxy reverso em
`deploy/nginx-wifi-space-mapper.conf`.

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp deploy/nginx-wifi-space-mapper.conf /etc/nginx/sites-available/wifi-space-mapper
sudo sed -i 's/mapa.suaempresa.com.br/SEU-DOMINIO/' /etc/nginx/sites-available/wifi-space-mapper
sudo ln -s /etc/nginx/sites-available/wifi-space-mapper /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d SEU-DOMINIO        # certificado gratuito
```

Depois disso os clientes acessam `https://SEU-DOMINIO`. O cookie de sessao e
`HttpOnly` + `SameSite=Lax`; com HTTPS ele tambem fica `Secure` pelo proprio
navegador.

**Firewall**

```bash
sudo ufw allow 8080/tcp        # acesso direto
# ou, com Nginx:
sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
```

---

## 5. Primeiros passos no cliente

1. Abra a URL do servidor.
2. Em **Criar conta**, informe nome, e-mail e senha (minimo 8 caracteres).
   A primeira conta se torna **administradora**.
3. Escolha o idioma no seletor **PT / EN / ES / CA** (canto superior direito).
   A preferencia fica salva no seu usuario.
4. Clique em **Novo** para criar um projeto. Informe o nome e as dimensoes do
   comodo (`largura,profundidade,altura` em metros, ex.: `6,8,3`).
5. Clique em **Mapear area**. A barra de progresso mostra as quatro fases:
   calibracao do enlace → varredura CSI → geometria (ToF + AoA) → reconstrucao 3D.
6. Ao terminar, a nuvem de reflexoes e a superficie aparecem no palco 3D e a
   captura fica guardada **dentro do projeto** (uma captura = um novo registro
   no historico daquele projeto).

### Navegacao 3D

| Acao | Controle |
|---|---|
| Orbitar 360° | botao **esquerdo** + arrastar |
| Deslocar (pan) | botao **direito** + arrastar |
| Zoom | roda do mouse |
| Navegacao livre (1ª pessoa) | botao **Navegacao livre** → `W/A/S/D` anda, `E/Q` sobe/desce |
| Vistas rapidas | **Topo / Frente / Lado / Isometrica** |
| Estilos | **Pontos / Aramado / Superficie** |
| Tamanho dos pontos | controle deslizante |

### Reguas dinamicas (extrair medidas)

Escolha uma regua e clique **dois pontos na nuvem**:

* **Largura** — trava Y e Z (mede so no eixo X), ideal para paredes frontais
* **Profundidade** — trava X e Z (mede no eixo Y)
* **Altura** — trava X e Y (mede no eixo Z), ideal para pe-direito e vaos
* **Distancia 3D** — medida livre entre quaisquer dois pontos
* **Vetor/polilinha** — clique varios vertices; **Enter** conclui, **Esc** cancela

Com **Encaixar na grade de 1 cm** ativo, cada ponto cai em multiplos de 1 cm —
util para medidas de obra. Cada medida e cada vetor ficam salvos e **vinculados
ao projeto** (e a captura em uso), com comprimento, deltas X/Y/Z, perimetro e
area projetada no piso quando o contorno e fechado.

---

## 6. Exportar os dados

No painel **Exportar** escolha:

**Conteudo**
* *Medidas e distancias* — todas as reguas do projeto (valor em m e cm, deltas, pontos)
* *Vetores e perimetros* — polilinhas com comprimentos por segmento, perimetro e area
* *Resumo das capturas* — uma linha por captura com metricas
* *Nuvem de pontos* — coordenadas + distancia ao roteador + intensidade
* *Malha 3D* — geometria da superficie reconstruida

**Formato**
* Tabelas: **CSV**, **JSON**, **PDF** (relatorio paginado) e **XLSX** se o
  `openpyxl` estiver instalado
* Nuvem: CSV, JSON ou **XYZ**
* Malha: **OBJ**, **PLY** ou **STL** (abre em Blender, MeshLab, AutoCAD, Revit)

**Campos** — marque exatamente as colunas que voce precisa; o arquivo sai
somente com elas. Cada geracao entra na lista de exportacoes do projeto com
link de download.

Atalhos diretos por captura:

```
GET /api/captures/{id}/download?fmt=obj     # malha
GET /api/captures/{id}/download?fmt=csv     # nuvem
GET /api/captures/{id}/download?fmt=json    # nuvem
```

---

## 7. Controle de usuarios

* Cada usuario tem **seus** projetos, capturas, medidas, vetores e exportacoes.
  Um usuario nunca ve dados de outro (o backend valida a propriedade em todas
  as rotas).
* A aba **Usuarios** (somente administradores) permite: alterar papel
  (*Usuario* / *Administrador*), ativar ou desativar contas e excluir usuarios.
* Senhas sao guardadas como **PBKDF2-HMAC-SHA256** com salt aleatorio e 120.000
  iteracoes; nunca em texto puro.
* Sessoes expiram em 30 dias e ficam registradas com IP e navegador.

---

## 8. Historico de areas mapeadas

A aba **Historico** lista todas as areas ja mapeadas com contadores de capturas,
medidas, vetores e triangulos, alem da linha do tempo de atividade (criacao de
projeto, capturas, medidas, vetores e exportacoes). Clicar num cartao abre o
projeto direto no mapa.

---

## 9. Enviar capturas de hardware real (opcional)

O portal já mapeia em modo de simulacao (validacao completa do pipeline). Para
usar CSI real, transmita para o servidor e registre a captura:

```bash
# UDP: ESP32-CSI / Nexmon enviando datagramas JSON na porta 5566
python tools/push_capture.py --server http://SEU-SERVIDOR:8080 \
    --email eu@dominio.com --password 'SenhaForte123' \
    --project 0 --name "Escritorio 3" --source udp --udp-port 5566 --seconds 12

# Serial: receptor ligado nesta maquina
python tools/push_capture.py --server http://SEU-SERVIDOR:8080 \
    --email eu@dominio.com --password 'SenhaForte123' \
    --project 0 --source serial --serial-port /dev/ttyUSB0
```

Com `--project 0` o script cria um **projeto novo** para a captura. Protocolos
aceitos (os mesmos do software original):

* UDP JSON: `{"rx":[x,y,z], "rot":[9 floats], "group":12, "csi":[[re,im],...]}`
* UDP binario: `<3d 9f 2I` + pares `int16` (escala 1/2048)
* Serial CSV: `t,x,y,z,r11..r33,re0,im0,re1,im1,...`

Uma ponte tambem pode publicar direto em
`POST /api/projects/{id}/captures/upload` (JSON com `points`, `mesh` e `metrics`).

---

## 10. Manutencao

```bash
# backup completo (banco + exportacoes)
tar czf backup-$(date +%F).tgz data/

# reiniciar
./run.sh --stop && ./run.sh --daemon
sudo systemctl restart wifi-space-mapper     # se usar systemd

# atualizar dependencias
.venv/bin/pip install -U -r requirements-web.txt

# ver a API e o estado
curl http://127.0.0.1:8080/api/health
abrir http://127.0.0.1:8080/api/docs         # documentacao interativa
```

**Variaveis de ambiente** (arquivo `.env`, criado na instalacao)

| Variavel | Padrao | Descricao |
|---|---|---|
| `WSM_HOST` | `0.0.0.0` | interface de escuta |
| `WSM_PORT` | `8080` | porta |
| `WSM_DATA` | `./data` | banco e exportacoes |
| `WSM_DB` | `$WSM_DATA/portal.db` | caminho do SQLite |

Para PostgreSQL/SQL Server, troque a camada `server/db.py` (todas as consultas
estao isoladas nele) mantendo as mesmas tabelas.

---

## 11. Limitacoes fisicas (leia antes de usar hardware real)

O CSI de hardware comercial atual e grosseiro para um mapa 3D fiel:

* com 160 MHz, a resolucao de atraso e `1/B ≈ 6,25 ns` → `≈ 1,9 m`; separar
  parede e piso exige mais de 1 GHz de banda;
* placas 802.11n/ac/ax expoem 30–256 subportadoras e 2–4 cadeias RF, limitando
  quantos percursos o MUSIC consegue resolver;
* descasamento de fase entre cadeias, CFO e AGC corrompem a fase CSI (a
  calibracao mitiga, nao elimina);
* a 2,4 GHz o comprimento de onda de 12,5 cm torna o canal sensivel a movimento.

**Na pratica**, espere uma nuvem de reflexoes esparsa (paredes grossas, moveis
grandes e superficies proximas) e erro de dezenas de centimetros a metros. A
malha reconstruida e qualitativa: use as reguas dinamicas para medir o que o
mapa capturou, nao como planta baixa certificada. Para planta precisa, o caminho
e UWB / radar FMCW / LiDAR — a arquitetura de ToF + AoA + MUSIC aqui
implementada continua sendo a correta, limitada pela largura de banda.

---

## 12. Solucao de problemas

| Sintoma | Causa provavel | Solucao |
|---|---|---|
| "Servidor indisponivel" no cliente | servidor parado ou porta bloqueada | `./run.sh --daemon`; libere a porta no firewall |
| Tela 3D preta | WebGL desativado | habilite aceleracao de hardware no navegador |
| `ImportError: open3d` | Open3D nao instalado | `./install.sh` (sem `--no-open3d`) ou aceite malha por voxels |
| Malha vazia | poucas reflexoes com o voxel atual | use qualidade **Precisa** e voxel menor (0,10 m) |
| PDF falhou | `reportlab` ausente | `.venv/bin/pip install reportlab` |
| XLSX nao aparece | `openpyxl` ausente | `.venv/bin/pip install openpyxl` |
| Esqueci a senha do admin | — | `sudo sqlite3 data/portal.db "UPDATE users SET pass_hash='x';"` e registre outra conta, ou recrie o banco |

---

## 13. Estrutura

```
wifi-space-mapper-web/
├── install.sh                  # instalador (deps, venv, banco, systemd, verificacao)
├── run.sh                      # iniciar / parar
├── requirements-web.txt
├── MANUAL_INSTALACAO.md        # este manual
├── server/
│   ├── app.py                  # API FastAPI + servidor do cliente
│   ├── db.py                   # SQLite: usuarios, sessoes, projetos, capturas, medidas, vetores
│   ├── auth.py                 # PBKDF2 + sessoes
│   ├── pipeline_bridge.py      # ponte csi_mapper (DSP + reconstrucao) + reserva
│   ├── surface.py              # malha por voxels e metricas de cobertura
│   └── exporters.py            # CSV / JSON / PDF / XLSX / OBJ / PLY / STL
├── web/
│   ├── index.html              # interface (mapa, historico, usuarios)
│   ├── css/app.css             # tema escuro + animacoes
│   ├── js/gl.js                # motor 3D WebGL (orbit, walk, picking, medidas)
│   ├── js/app.js               # logica do cliente + reguas dinamicas + exportacao
│   ├── js/i18n.js              # tradutor
│   └── i18n/{pt,en,es,ca}.json # dicionarios
├── deploy/
│   ├── wifi-space-mapper.service
│   └── nginx-wifi-space-mapper.conf
├── tools/push_capture.py       # envia capturas de hardware real
├── csi_mapper/                 # pacote original de DSP (reutilizado sem alteracoes)
└── data/                       # banco + exportacoes (criado na instalacao)
```

---

## 14. API resumida

```
GET    /api/health
POST   /api/auth/register | /login | /logout
GET    /api/auth/me          PATCH /api/auth/me
GET    /api/users            PATCH/DELETE /api/users/{id}
GET    /api/projects         POST /api/projects
GET    /api/projects/{id}    PATCH/DELETE /api/projects/{id}
GET    /api/projects/{id}/history
POST   /api/projects/{id}/captures/run       (executa o pipeline CSI)
POST   /api/projects/{id}/captures/upload    (captura de hardware)
GET    /api/projects/{id}/captures
GET    /api/captures/{id}    DELETE /api/captures/{id}
GET    /api/captures/{id}/download?fmt=obj|csv|json|xyz
GET/POST /api/projects/{id}/measurements     PATCH/DELETE /api/measurements/{id}
GET/POST /api/projects/{id}/vectors          PATCH/DELETE /api/vectors/{id}
GET    /api/export/fields
POST   /api/projects/{id}/export             GET /api/exports/{id}/download
GET    /api/history
```
