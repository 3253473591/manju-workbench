FROM node:22-alpine AS build
WORKDIR /app

# 用镜像自带的 npm，不用 corepack 拉 pnpm：
# 新版 pnpm 默认拒绝执行依赖的 build 脚本（ERR_PNPM_IGNORED_BUILDS），在镜像里会直接失败。
COPY package.json ./
RUN npm install --no-audit --no-fund

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
