# postgres:16-alpine + pgvector —— 与 live db 相同的 alpine/musl 基础，
# 仅多编译 pgvector 扩展，避免 glibc 镜像换底带来的 collation 风险。
# with_llvm=no：官方 alpine 镜像的 PG 期望 clang-21 生成 bitcode（JIT 内联
# 优化），缺之不影响扩展功能，跳过以免钉死 clang 版本。
FROM postgres:16-alpine AS build
RUN apk add --no-cache --virtual .build-deps build-base curl postgresql-dev \
    && curl -sSL https://api.github.com/repos/pgvector/pgvector/tarball/v0.8.0 -o /tmp/pgvector.tar.gz \
    && mkdir -p /tmp/pgvector && tar -xzf /tmp/pgvector.tar.gz -C /tmp/pgvector --strip-components=1 \
    && cd /tmp/pgvector && make with_llvm=no && make with_llvm=no install DESTDIR=/out \
    && ls /out/usr/local/lib/postgresql/vector.so /out/usr/local/share/postgresql/extension/vector.control

FROM postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea
COPY --from=build /out/usr/local/ /usr/local/
