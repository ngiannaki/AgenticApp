# The public entrance for the edge/tunnel compose profiles: builds the
# Next.js static export (frontend repo, passed as the `frontend` build
# context) and serves it with Caddy, which also proxies /api to the API.
FROM node:22-alpine AS build
WORKDIR /src
COPY --from=frontend package.json package-lock.json ./
RUN npm ci
COPY --from=frontend . .
# Same origin as the API (Caddy proxies /api), so no base URL.
ENV NEXT_PUBLIC_API_BASE_URL=""
RUN npm run build

FROM caddy:2.10-alpine
COPY docker/web/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /src/out /srv
