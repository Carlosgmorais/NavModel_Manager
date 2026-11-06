# NavModel Manager — 0.0.12

Aplicativo desktop Windows para visualização e coordenação de maquetes industriais 3D federadas, com PySide6 e VTK/OpenGL.

A revisão 0.0.12 traz importantes avanços na ferramenta de Cortes (Sectioning), suporte completo a arquivos DWG industriais massivos (AutoCAD Plant 3D) e otimizações globais de desempenho:
- **Ferramenta de Cortes (Sectioning):**
  - Adicionado botão **Mover (Eixos)** para habilitar ou desabilitar o gizmo dos eixos de coordenadas para translação do corte.
  - O corte agora surge posicionado **mais próximo da tela** (no ponto focal da câmera atual e com dimensões compatíveis com o zoom) em vez de abranger todo o plano distante do modelo.
  - Adicionado botão **Trazer à vista** para reposicionar o plano de corte imediatamente no centro da visão atual do operador.
  - Atalho de **Cortes** incluído no grupo Ferramentas da aba principal **Início**.
- **Motor DWG 3D Open Source (Plant 3D / ACIS):**
  - Elevação do limite de seção no `acadrust` de 1 GB para 8 GB (`MAX_SECTION_SIZE = 8 GiB`), permitindo abrir arquivos gigantes como `DESPOEIRAMENTO DELTA DUCON.dwg` (1,15 GB de seção ACIS) e `105-40-230-DFO-702_R3_MODELO 3D.dwg`, recuperando 100% dos sólidos 3D.
  - Tolerância de tesselação adaptativa para modelos industriais complexos, evitando explosão desnecessária de triângulos e acelerando a abertura em até 50x.
  - Proteção contra falhas de triangulação Delaunay (`catch_unwind`), garantindo estabilidade contínua dos threads de conversão.
  - Relato de progresso contínuo de sólidos e aumento generoso dos timeouts para arquivos pesados.
- **Otimização Global e GPU:**
  - Fast-path de agrupamento por camada e cor (`FAST_LAYER_BATCH`) no pipeline GPU para modelos com centenas/milhares de peças, eliminando o gargalo de hashing e garantindo navegação a 60 FPS com baixo consumo de memória RAM.

## Identidade e interface

A identidade foi extraída dos PNGs aprovados em `resources/reference`. A geometria do símbolo NV não foi redesenhada. `resources/asset-provenance.json` registra origem, recorte e hash.

- Ribbon nativa com menu NV separado de Início; Append e Refresh em azul.
- Árvore à esquerda e propriedades à direita; painéis redimensionáveis e destacáveis.
- Fontes Segoe UI; paleta azul-marinho; botões desenhados como vetores Qt, não imagens de mockups.
- Cubo CAD clicável, com faces, arestas e cantos: 26 orientações. Duplo clique restaura isométrica e Ctrl+Z desfaz a vista. O foco, a distância, a escala ortogonal e a projeção são preservados.
- Splash com fundo sólido, wireframe e logo extraídos das referências; duração visível mínima de 5 segundos.
- Ícone Windows de 16, 24, 32, 48, 64, 128 e 256 px; metadados e atalhos NavModel Manager.
- O viewport não mostra mensagens de boas-vindas.

## Controles

| Ação | Controle |
|---|---|
| Append | Ctrl+I |
| Refresh | F5; preserva câmera e visibilidade |
| Selecionar | Ativar botão Selecionar e clicar, ou Shift+clique |
| Olhar | Arrastar botão esquerdo |
| Pan | Arrastar botão do meio |
| Orbitar | Shift + botão do meio |
| Zoom | Roda do mouse |
| Desfazer câmera | Ctrl+Z |
| Ocultar / mostrar seleção | Ctrl+H |
| Medir | M |
| Abrir / salvar projeto | Ctrl+O / Ctrl+S |

## Cortes e medição

Na aba **Sectioning Tools**, ative o corte e escolha **Plano** ou **Box**.
Para um plano, escolha X, Y ou Z, alinhe à vista, ajuste à seleção ou inverta o lado visível.
Arraste as setas vermelha, verde e azul para mover, ou use **Girar** e arraste os anéis.
No Box, as seis setas de face ajustam apenas a face escolhida. O painel **Valores**
permite informar posição, ângulos e dimensões em metros.

Na medição, o quadrado indica a captura no modelo. O painel oferece face, vértice
e aresta da malha. Clique nos dois pontos para obter distância e ΔX/ΔY/ΔZ com
sinal em metros. O índice espacial reutiliza a geometria e ignora a parte removida pelo corte.

## Publicar Maquete Web

Abra **Saída → Publicar Maquete Web**. A análise mostra objetos e grupos do visor,
triângulos, tamanho dos arquivos de origem e estimativas de volume e memória.
Os limites iniciais do HTML são 20.000 objetos, 1,5 milhão de triângulos, 80 MiB
de arquivo estimado e 768 MiB de memória estimada. Ajuste os valores no diálogo
conforme a capacidade do computador do destinatário.

- **HTML único** abre diretamente em navegador com WebGL, inclusive offline.
- **EXE portátil** abre no navegador pelo endereço local `127.0.0.1`. O arquivo Windows
  x64 contém a geometria compactada e carrega blocos progressivamente. O destinatário
  precisa apenas do arquivo e de um navegador com WebGL. O servidor local encerra
  após três minutos sem atividade.

Ambos preservam posicionamento, grupos, visibilidade, cores e propriedades disponíveis.
O visualizador oferece órbita, pan, zoom, plano, Box e medição por face. A publicação
herda os limites do leitor de origem. Avisos relevantes do DWG são apresentados
quando disponíveis. Maquetes grandes ainda precisam caber na memória do navegador.
O EXE não tem assinatura digital e pode exigir liberação pela política de TI do destinatário.

O formato `.pvproj` permanece compatível com os projetos PlantView existentes. A revisão 0.0.10 ajusta cores e gráficos proxy no leitor DWG/Rust e a identificação de instâncias GPU. Os leitores STEP/IFC, a navegação e as coordenadas originais são preservados. A aba Animação é reservada e está explicitamente desabilitada: esta entrega não acrescenta um motor de animação.

## Compilar e validar no Windows

Com Python 3.12, dependências de `requirements-lock.txt`, SDK .NET e Inno Setup 6:

```powershell
.\build-windows.ps1 -UseExistingEngine
```

A opção acima verifica o hash do motor DWG distribuído no repositório e o preserva. Sem a opção, o script recompila o motor Rust usando Cargo, como na versão anterior.

O processo executa 3 testes de lógica GPU, 4 testes de fidelidade CAD, 4 testes de interface/cubo e 10 testes CAD/projeto/Refresh, empacota a aplicação, executa o smoke test do executável e gera o instalador. O instalador é copiado para a Área de Trabalho por padrão; `-CopyTo` permite outro destino.

- Aplicação: `dist/NavModel Manager/NavModel Manager.exe`
- Instalador: `release/NavModel_Manager_0.0.11_Setup_x64.exe`

O instalador é um único `.exe`. A aplicação instalada contém dependências ao lado do executável, evitando extração de um pacote grande a cada inicialização. O pacote inclui as licenças de terceiros; caminhos excepcionalmente longos são encurtados e documentados no índice `licenses/license-path-index.json`.

## Limites da validação

Os testes usam os modelos de exemplo DWG, STEP e IFC e verificam coordenadas conhecidas. Foi validado também um DWG real de escada industrial. Isso não substitui a avaliação de toda maquete grande e de todos os tipos de entidades CAD. O agrupamento GPU existente continua determinando a granularidade selecionável. Não há certificado de assinatura digital configurado no projeto.

## Revisão 0.0.10 — fidelidade DWG

A leitura segue o índice ativo do datastore AcDs para associar cada sólido à sua geometria vigente. Revisões antigas continuam presentes fisicamente em alguns DWGs e não podem substituir o registro ativo. Geometrias em páginas `blob01` são recuperadas pelos identificadores e tamanhos de suas páginas, inclusive quando o tamanho do segmento informa somente o cabeçalho. A regra não depende de nomes de arquivos, peças ou projetos.

Objetos Plant 3D que referenciam blocos nativos são reconstruídos a partir desses blocos, com verificação da posição contra os limites dos gráficos salvos. Isso recupera a geometria real dos componentes, em vez de aproximar flanges, válvulas e filtros por círculos. A curva superior da coifa foi recuperada dessa forma de armazenamento paginado, sem desenhar uma peça substituta.

Cores explícitas, incluindo branco e cinza, são preservadas. ByBlock e ByLayer mantêm seus contextos próprios; atributos de cor de faces ACIS têm precedência sobre a cor geral do sólido. Cores ACI/True Color e transformações dos gráficos proxy também são preservadas. A identificação de instâncias GPU compara todos os vértices e a conectividade, mantendo cópias transladadas e separando peças diferentes.

Os testes nativos incluem revisões obsoletas, páginas múltiplas, identificadores de 64 bits, diretórios truncados, aberturas e cores por face. Execute `cargo test --release --manifest-path dwg-reader/Cargo.toml` para reproduzi-los. Os modelos reais servem como validação adicional; não há exceções por nome no leitor.

O relatório diferencia sólidos incompletos e faces ACIS não reconstruídas. Entidades proprietárias sem bloco nativo ou geometria de superfície compatível continuam dependentes dos dados proxy disponíveis no arquivo. As coordenadas não são realinhadas por aproximação.

Correção pontual do Doghouse: perfis estruturais Plant com seções coincidentes são expandidos pelo eixo salvo antes da triangulação das tampas. Validação no DH-100001_DOGHOUSE: 38 perfis recuperados e 3.970 malhas restantes idênticas; nenhum sólido ou face incompleto nesse arquivo.
