# Landing page — Code Watcher

Página única e estática (`index.html`, sem build). As fontes vêm do Google
Fonts; todo o resto (CSS, JS, o skyline em Canvas) está embutido no arquivo.

## Deploy no Coolify

1. **New Resource → Docker file** (não "Static Site" — este repo usa nginx
   próprio para os headers de cache).
2. **Source:** este repositório · branch `main`.
3. **Base Directory:** `/landing`
4. **Dockerfile Location:** `/landing/Dockerfile` (ou só `Dockerfile` se o
   Base Directory já for `/landing`).
5. **Port:** `80`
6. **Domains:** adicione o alias, ex. `https://codewatcher.seudominio.com`.
   O Coolify provisiona o TLS (Let's Encrypt) sozinho — só garanta que o
   registro DNS `A`/`CNAME` do alias aponta pro servidor do Coolify antes.
7. **Deploy.** Cada `git push` na branch redeploya (habilite o webhook do
   Coolify no repo, ou use "Deploy on push").

### Alternativa sem Dockerfile

Dá pra usar **Static Site** (buildpack nixpacks) com Base Directory
`/landing` e Publish Directory `.` — mais simples, mas você perde o controle
dos headers do `nginx.conf`.

## Rodar local

```bash
docker build -t cw-landing ./landing
docker run --rm -p 8080:80 cw-landing
# http://localhost:8080
```

Ou sem Docker, só abrir `landing/index.html` no navegador.

## Editar

O arquivo `index.html` é gerado a partir do design original. Para mudar
texto/cores, edite direto — é HTML/CSS/JS puro, sem passo de build.
