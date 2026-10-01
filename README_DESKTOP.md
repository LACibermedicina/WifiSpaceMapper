# Mapeador 3D de Espaço por CSI Wi-Fi

Software standalone em Python que reconstrói automaticamente o volume do cômodo
em volta de um roteador Wi-Fi a partir de **CSI (Channel State Information)** e
permite **navegação 3D total** (órbita, pan, zoom e primeira pessoa).

* Interface: **PySide6** (Qt 6)
* Renderização 3D interativa: **VisPy** (`SceneCanvas` embutido no Qt)
* Processamento de geometria: **Open3D** (normais, Poisson, Alpha Shapes)
* DSP: **NumPy / SciPy** (MUSIC 2D de azimute × elevação, MUSIC 1D de ToF)
* Agrupamento: **scikit-learn** (DBSCAN)

---

## 1. Como executar

```bash
pip install -r requirements.txt
python -m csi_mapper.main              # modo demonstração (sem hardware)
python -m csi_mapper.main --fast       # execução imediata, sem o ritmo de 3 s + 7 s
python -m csi_mapper.main --room 6,8,3 # cômodo 6 x 8 x 3 m
python -m csi_mapper.main --source udp --udp-port 5566
```

Teste automatizado (headless, sem GUI):

```bash
python selftest.py            # gera métricas no terminal
python selftest.py --png mapa.png   # também salva um render Open3D
```

---

## 2. Controles de navegação 3D

| Ação | Controle |
|---|---|
| **Orbitar / rotacionar 360°** | Botão **esquerdo** + arrastar |
| **Mover / transladar (pan)** | Botão **direito** + arrastar, ou **Shift + esquerdo** + arrastar |
| **Zoom in / out** | **Roda do mouse** (ou botão do meio arrastando) |
| **Navegação livre (1ª pessoa)** | Botão **"Navegação Livre (WASD)"** → `W/A/S/D` anda, `E/Q` sobe/desce, arrastar com o esquerdo para olhar |
| **Resetar câmera / centralizar no roteador** | Botão `⟲` na barra ou barra lateral (atalho: `Espaço`) |
| **Vistas pré-definidas** | `⊞ Topo` (F2), `▤ Frente` (F3), `▥ Lado` (F4), `◇ Isométrica` (F5) |
| **Estilos de visualização** | Combo na barra: Nuvem de Pontos / Wireframe / Malha Sólida (atalhos `P`, `W`, `M`) |

A origem `(0,0,0)` é o **roteador**, desenhado com corpo escuro, antenas neon
ciano e um halo luminoso. Os eixos cartesianos são coloridos
(**X = vermelho, Y = verde, Z = azul**) com setas, e o piso possui grade métrica
com rótulos em metros (`x=+1 m`, `y=-2 m`, …).

---

## 3. Fluxo do botão "Mapear Espaço"

1. **Calibração (0–3 s)** — pacotes estáticos: estima-se o percurso direto
   (LoS) por MUSIC, extrai-se a **fase residual por cadeia RF** (calibração de
   descasamento entre cadeias) e mede-se a SNR do enlace.
2. **Varredura (3–10 s)** — o receptor percorre uma trajetória elíptica de
   três níveis; para cada posição:
   * **AoA**: pseudospectro MUSIC 2D sobre (azimute, elevação) com suavização
     em frequência;
   * **ToF**: pseudospectro MUSIC 1D sobre as subportadoras decimadas, com
     suavização espacial *forward-backward*;
   * conversão para coordenadas cartesianas resolvendo a interseção
     **raio × elipsoide de caminho constante** `|RX + t·û − TX| + t = L`.
3. **Geração 3D automática** — *voxel downsample* → *Statistical Outlier
   Removal* → **DBSCAN** (scikit-learn) → normais orientadas ao roteador →
   **Poisson Surface Reconstruction** (fallback **Alpha Shapes** → casco
   convexo) → corte pela caixa do cômodo → decimação quadrática → coloração por
   distância ao roteador (azul perto → vermelho longe).

Toda a execução ocorre em `QThread` com sinais de progresso; a interface nunca
trava e o botão **Cancelar** interrompe o pipeline.

---

## 4. Módulo de simulação (mock)

Sem hardware, o **Modo de Demonstração** é ativado automaticamente. Ele **não**
devolve pontos do cômodo: sintetiza o canal MIMO-OFDM real de uma sala de
**4 m × 5 m × 2,8 m** (roteador no centro) por traçado de raios de primeira
ordem (método da imagem, 6 superfícies + obstáculos), montando

```
H[m,k] = Σ_p  a_p · exp(−j·2π·f_k·L_p/c) · exp(+j·2π·f_k·(p_m·û_p)/c)
```

com desvio de fase/ganho por cadeia RF, CFO por pacote e ruído AWGN na SNR
configurada. O pipeline de DSP recupera depois ToF e AoA **às cegas**, como
faria com hardware real.

---

## 5. Hardware real

O software tenta, em ordem: **UDP → Serial → Mock**.

**UDP (JSON, um datagrama por pacote CSI)**

```json
{"rx":[x,y,z], "rot":[9 floats row-major], "group":12, "csi":[[re,im], ...]}
```

`csi` possui `n_ant × n_sub` pares (ordem antena-maior). Também é aceito o
formato binário compacto `<3d 9f 2I` + `n_ant·n_sub` pares `int16` (escala
1/2048).

**Serial (linha CSV, estilo ESP32-CSI)**

```
t, x, y, z, r11..r33 (9 floats), re0, im0, re1, im1, ...
```

A pose `(x, y, z)` + rotação de cada rajada é o pressuposto de qualquer sistema
de mapeamento Wi-Fi por CSI (o receptor móvel conhece a própria odometria).

---

## 6. Limite físico (leia antes de usar hardware real)

**O CSI de hardware comercial atual é grosseiro demais para um mapa 3D fiel de
um cômodo.** As razões são objetivas:

* a resolução de atraso é limitada pela largura de banda: com 160 MHz,
  `Δτ ≈ 1/B = 6,25 ns` → `≈ 1,9 m` de resolução de caminho; separar parede e
  piso exige > 1 GHz de banda (Wi-Fi futuro / radar UWB);
* as placas 802.11n/ac/ax expõem de **30 a 256 subportadoras** e **2 a 4
  cadeias RF** (no máximo 3–8 elementos de antena; o padrão 802.11ax chega a 8
  no lado AP), o que limita severamente o **número de percursos resolvíveis**
  pelo MUSIC;
* **descasamento de fase entre cadeias RF**, CFO e erros de AGC corrompem a
  fase CSI; a calibração implementada mitiga, mas não elimina;
* com 2,4 GHz, o comprimento de onda de 12,5 cm torna o canal extremamente
  sensível a movimento — cada posição exige pacotes estáticos.

Consequências práticas: com hardware real você deve esperar uma **nuvem de
reflexões esparsa** (paredes grossas, móveis grandes e as superfícies mais
próximas), erro da ordem de **dezenas de centímetros a metros**, e geometria
**qualitativa** — não uma planta baixa precisa. O modo de simulação existe
justamente para validar todo o pipeline (DSP, reconstrução, visualização) de
ponta a ponta sem esse gargalo físico, e os painéis de estatísticas comparam a
malha reconstruída com a geometria real para quantificar o erro.

Para mapeamento 3D fiel hoje, o caminho realista é **UWB / radar FMCW / LiDAR**;
o subsistema CSI aqui implementado é a arquitetura correta (ToF + AoA + MUSIC +
fusão geométrica), limitado pela largura de banda do padrão.

---

## 7. Estrutura do código

```
wifi_space_mapper/
├── csi_mapper/
│   ├── config.py          # geometria, rádio, DSP e parâmetros de reconstrução
│   ├── room.py            # cômodo, planos refletores, traçado de raios, malhas auxiliares
│   ├── csi_source.py      # MockCSISource (simulação) + UDP + Serial
│   ├── dsp.py             # calibração, MUSIC 2D (AoA), MUSIC 1D (ToF), geometria
│   ├── reconstruction.py  # voxel/SOR/DBSCAN/Poisson/Alpha Shapes (Open3D)
│   ├── viewer.py          # Viewport3D (VisPy) + OrbitPanCamera + WalkCamera
│   ├── colors.py          # gradiente de distância e cores da cena
│   ├── main.py            # janela PySide6, painéis, QThread, estilo
│   └── __main__.py
├── selftest.py            # teste headless do pipeline + métricas (+ PNG opcional)
└── requirements.txt
```
