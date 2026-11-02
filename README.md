# NavModel Manager — 0.0.7

Aplicativo desktop Windows para visualização e coordenação de maquetes industriais 3D federadas, com PySide6 e VTK/OpenGL.

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

O formato `.pvproj` permanece compatível com os projetos PlantView existentes. Não houve mudança no leitor DWG/STEP, motor Rust, coordenadas, buffers GPU ou estratégia de agrupamento. A aba Animação é reservada e está explicitamente desabilitada: esta entrega não acrescenta um motor de animação.

## Compilar e validar no Windows

Com Python, dependências instaladas e Inno Setup 6:

```powershell
.\build-windows.ps1 -UseExistingEngine
```

A opção acima verifica o hash do motor DWG distribuído no repositório e o preserva. Sem a opção, o script recompila o motor Rust usando Cargo, como na versão anterior.

O processo executa 3 testes de lógica GPU, 4 testes de interface/cubo e 10 testes CAD/projeto/Refresh, empacota a aplicação, executa o smoke test do executável e gera o instalador. O instalador é copiado para a Área de Trabalho por padrão; `-CopyTo` permite outro destino.

- Aplicação: `dist/NavModel Manager/NavModel Manager.exe`
- Instalador: `release/NavModel_Manager_0.0.7_Setup_x64.exe`

O instalador é um único `.exe`. A aplicação instalada contém dependências ao lado do executável, evitando extração de um pacote grande a cada inicialização. O pacote inclui as licenças de terceiros; caminhos excepcionalmente longos são encurtados e documentados no índice `licenses/license-path-index.json`.

## Limites da validação

Os testes usam os modelos de exemplo DWG, STEP e IFC e verificam coordenadas conhecidas. Foi validado também um DWG real de escada industrial. Isso não substitui a avaliação de toda maquete grande e de todos os tipos de entidades CAD. O agrupamento GPU existente continua determinando a granularidade selecionável. Não há certificado de assinatura digital configurado no projeto.
