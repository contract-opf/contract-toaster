# DTS frontend image — builds the SPA in password-auth mode and serves it via
# nginx, which also reverse-proxies the API so the browser talks same-origin
# (no CORS). Built from the REPO ROOT context.
#
# Build:  docker build -f deploy/dts/frontend.Dockerfile -t contract-toaster-dts-frontend .
FROM node:20-slim AS build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Password-auth build: Amplify/Cognito is not configured; same-origin API base.
ENV VITE_AUTH_MODE=password
ENV VITE_API_BASE_URL=""
RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/dts/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/frontend/dist /usr/share/nginx/html
EXPOSE 8080
