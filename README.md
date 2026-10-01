# Wi-Fi CSI Space Mapper — portal web autoinstalavel

Portal web para mapeamento 3D de ambientes por **CSI Wi-Fi**, com controle de
usuarios, uma captura por projeto, reguas dinamicas de medida, historico de
areas mapeadas e exportacao de dados (CSV / JSON / PDF / XLSX / OBJ / PLY / STL).
Interface traduzida em **portugues, ingles, espanhol e catalao**.

## Instalar (servidor Linux)

```bash
chmod +x install.sh run.sh
./install.sh                  # cria o venv, instala tudo e inicializa o banco
./run.sh --daemon             # sobe o servidor (padrao http://0.0.0.0:8080)
```

Abra `http://IP-DO-SERVIDOR:8080` em qualquer cliente e crie a conta inicial
(ela vira **administradora**).

Opcoes: `./install.sh --port 9000`, `--no-open3d`, `sudo ./install.sh --systemd`,
`--admin eu@dominio.com --admin-pass 'SenhaForte123'`.

**Manual completo:** [MANUAL_INSTALACAO.md](MANUAL_INSTALACAO.md)
**API interativa:** `http://IP-DO-SERVIDOR:8080/api/docs`

## Verificar a instalacao

```bash
.venv/bin/python tools/smoke_test.py http://127.0.0.1:8080
```

Executa registro, login, projeto, captura CSI, as quatro reguas, vetor fechado,
vinculacao ao projeto, exportacoes e isolamento entre usuarios.
