# Handoff: CodeWatcher — Mapa 3D estilo "Git City"

## Overview
Nova aba dentro do CodeWatcher (`ui.html`), ao lado de Feed/Backlog: um mapa 3D estilo skyline noturno pixelizado (referência: Git City), onde cada distrito é um projeto monitorado, cada torre é um arquivo, e cada bloco empilhado na torre é um evento (commit/PR).

## About the Design Files
`CodeWatcher Redesign.dc.html` é referência HTML de alta fidelidade com a cena three.js funcional — não copiar como código de produção, **recriar dentro de `ui.html`** (HTML/CSS/JS puro via pywebview), preservando toda lógica/API existente. `screenshot.png` mostra o resultado visual esperado.

## Fidelity
High-fidelity visual: paleta, câmera, densidade da cidade e estilo dos marcadores devem seguir a spec abaixo — é a característica central do pedido (o usuário pediu explicitamente o estilo "Git City": skyline denso, janelas brilhantes, câmera baixa quase no nível do horizonte, marcadores dourados flutuantes sobre construções críticas).

## Conceito da cena
- **Distrito** = projeto/pasta monitorada. Sem plataforma/base visível — só um cluster denso de torres na região daquele projeto, com um rótulo de texto dourado flutuando acima.
- **Torre** = um arquivo dentro do projeto. Posição em grid com leve jitter aleatório (para não ficar em grade perfeita, efeito mais orgânico de cidade).
- **Bloco** = um evento individual (commit ou PR) nesse arquivo, empilhado cronologicamente (mais antigo na base). Altura da torre = nº de eventos daquele arquivo.
- **Torres de preenchimento**: ~2400 torres não-interativas, geradas ao redor de todos os distritos (fora da área ocupada por eles), com altura decrescente conforme a distância do centro — criam o efeito de skyline denso, sem buracos, preenchendo o quadro de ponta a ponta.
- **Marcador crítico**: um octaedro dourado/vermelho flutuando (com anel decorativo) acima de qualquer torre que tenha ao menos um evento de severidade alta — gira lentamente, sempre visível independente da distância da câmera.

## Cor e material — CRÍTICO para o visual
- Todas as torres (projeto + preenchimento) usam **material não-iluminado (`MeshBasicMaterial`)** com uma textura de "janelas" gerada em canvas: grade de pequenos quadrados, ~65% "aceso" (cor viva translúcida) e ~35% "apagado" (quase preto), simulando prédio com janelas iluminadas à noite. Isso é intencional — luz de cena (`MeshStandardMaterial`) deixa as torres quase invisíveis contra o fundo escuro; o material não-iluminado garante brilho constante.
- Cor da janela por torre: se algum evento do arquivo é severidade alta → vermelho (`#e05a4e`); senão se média → amarelo (`#d9b354`); senão → azul (`#5b8def`). Torres de preenchimento usam verde (`#3fae6c`), sem interatividade.
- Fundo da cena: azul-marinho quase preto `#060a14`, com `FogExp2` densidade 0.006 na mesma cor (esmaece o horizonte sem apagar as torres próximas).
- Chão: plano `#0a0e18` + `GridHelper` sutil (linhas `#1a2540`/`#121a2c`) para dar referência de profundidade.
- Rótulo de distrito: texto em canvas, fonte mono 700, cor dourada `#e8c77a`, glow leve (`shadowBlur`), renderizado como sprite sempre de frente para a câmera.

## Câmera e enquadramento
- `PerspectiveCamera` FOV 65, posição inicial baixa e próxima — ~(2, 15, 22), olhando para (0, 4, 0) — ângulo quase no nível do horizonte (não visão de topo), para que o skyline preencha o quadro de ponta a ponta como na referência, sem grandes áreas vazias de céu.
- `OrbitControls`: damping ativado, `maxPolarAngle` levemente abaixo de 90° (não deixa a câmera ir para debaixo do chão), `minDistance` ~10, `maxDistance` ~150.

## Interações
1. **Câmera livre 3D** (rotação/pan/zoom via OrbitControls).
2. **Clique num bloco**: raycasting identifica o bloco exato; abre painel no canto superior direito com projeto, arquivo, tipo (Commit/PR), mensagem, horário, severidade + botão "Ver no feed" (troca para aba Feed já filtrada por aquele projeto/arquivo).
3. **Filtro por projeto**: chips na barra inferior (um por projeto + "Todos projetos"). Oculta blocos fora do projeto via `visible=false` (não remove geometria).
4. **Filtro por severidade**: chips análogos ("Todas severidades"/Alta/Média/Baixa).
5. **Timelapse**: slider de 1 até o índice cronológico máximo — só ficam visíveis blocos com `order <= valor`, simulando o crescimento da cidade no tempo.
6. Os três controles vivem numa única barra fixa inferior — não espalhados sobre a cena.

## Estilo da UI ao redor do mapa
- Barra inferior e chips em tema preto/dourado (não o azul/roxo usado no resto do app): fundo quase preto translúcido (`rgba(8,11,18,0.92)`), borda dourada sutil (`rgba(232,199,122,0.35)`), labels em uppercase mono cinza-azulado (`#8a93a6`), chip ativo = fundo dourado sólido (`#e8c77a`) com texto escuro (`#111826`), chip inativo = fundo escuro translúcido com borda dourada tênue.
- Painel de detalhe do bloco selecionado: mantém o card-style padrão do resto do app (radius, fundo `--panel-2`, tag de projeto igual ao feed) — não precisa do tema dourado, é conteúdo textual.

## Requisitos técnicos
- Three.js (module) + `OrbitControls` via import map, versão pinada 0.184.0.
- `renderer.toneMapping = ACESFilmicToneMapping`, `toneMappingExposure ~1.15`.
- Textura de janelas gerada uma vez por cor (cache) via `<canvas>` + `CanvasTexture`, `wrapS/wrapT = RepeatWrapping`.
- Ao trocar de aba, forçar resize do renderer (o container fica display:none/oculto quando outra aba está ativa) — usar `requestAnimationFrame` + um `setTimeout` de reforço.
- Raycasting restrito aos meshes interativos (torres de projeto), não às torres de preenchimento.
- Manter todas as funções JS existentes (`toggleMaster`, `filterByProject`, `resolveBacklogItem`, chamadas a `window.pywebview.api.*`) intactas.

## Files
- `CodeWatcher Redesign.dc.html` — referência completa com a cena 3D funcional (nova versão, estilo Git City).
- `screenshot.png` — captura do resultado (aba Mapa 3D).
- Implementar em `ui.html` do repo `ndmg-dev/CodeWatcher` (branch `main`).
