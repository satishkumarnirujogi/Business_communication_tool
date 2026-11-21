# Frontend: builds the patient chat, admin dashboard and widget, then serves them with nginx, which also
# forwards /api, /webhooks and /branding to the backend.
FROM node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html/ui
EXPOSE 80
