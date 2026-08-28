# Landing page estática do Code Watcher — servida por nginx.
# Coolify: crie um recurso "Dockerfile" apontando pra este repo,
# com Base Directory = /landing. A porta exposta é 80.
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html favicon.svg favicon.ico /usr/share/nginx/html/

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
