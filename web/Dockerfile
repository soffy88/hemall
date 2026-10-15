# Hemall Web (Next.js) 生产 Dockerfile
# 多阶段构建 + standalone 输出，非 root 运行

# ── 依赖阶段 ────────────────────────────────────────────────────────────
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS deps
WORKDIR /app
COPY package.json package-lock.json ./
COPY vendor ./vendor
RUN npm ci

# ── 构建阶段 ────────────────────────────────────────────────────────────
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* 在构建期内联进 bundle，必须在这一步注入，运行时再设无效
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build

# ── 运行阶段 ────────────────────────────────────────────────────────────
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS runner
WORKDIR /app
ENV NODE_ENV=production

RUN addgroup -g 1001 -S hemall && adduser -u 1001 -S hemall -G hemall

COPY --from=builder /app/public ./public
COPY --from=builder --chown=hemall:hemall /app/.next/standalone ./
COPY --from=builder --chown=hemall:hemall /app/.next/static ./.next/static

USER hemall

ENV PORT=3000
# 事故修复 (2026-10-15): 健康检查打 127.0.0.1:3000 被拒 (Connection refused)。
# 根因是 Docker 会在容器运行时注入自己的 HOSTNAME (= 容器 ID / 容器 IP),
# 覆盖掉镜像层的 `ENV HOSTNAME=0.0.0.0`; Next standalone 的 server.js 读
# process.env.HOSTNAME 去 bind, 于是只绑到容器 IP, loopback 上没人监听。
# 下面 CMD 用 shell 在 exec 前就地赋值 —— shell 设的环境变量一定压过 Docker
# 注入的那个, 0.0.0.0 绑定才真正生效。健康检查同时兜底探测容器 IP。
ENV HOSTNAME=0.0.0.0
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget --spider -q http://127.0.0.1:3000/ \
        || wget --spider -q "http://$(hostname -i | awk '{print $1}'):3000/" \
        || exit 1

CMD ["sh", "-c", "HOSTNAME=0.0.0.0 exec node server.js"]
