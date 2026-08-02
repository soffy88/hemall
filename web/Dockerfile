# Hemall Web (Next.js) 生产 Dockerfile
# 多阶段构建 + standalone 输出，非 root 运行

# ── 依赖阶段 ────────────────────────────────────────────────────────────
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
COPY vendor ./vendor
RUN npm ci

# ── 构建阶段 ────────────────────────────────────────────────────────────
FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* 在构建期内联进 bundle，必须在这一步注入，运行时再设无效
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build

# ── 运行阶段 ────────────────────────────────────────────────────────────
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production

RUN addgroup -g 1001 -S hemall && adduser -u 1001 -S hemall -G hemall

COPY --from=builder /app/public ./public
COPY --from=builder --chown=hemall:hemall /app/.next/standalone ./
COPY --from=builder --chown=hemall:hemall /app/.next/static ./.next/static

USER hemall

ENV PORT=3000
ENV HOSTNAME=0.0.0.0
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD wget --spider -q http://127.0.0.1:3000/ || exit 1

CMD ["node", "server.js"]
