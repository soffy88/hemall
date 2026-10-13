#!/bin/sh
# Alertmanager 容器入口 (audit P1-6)
#
# Alertmanager 原生不支持配置里的环境变量插值，所以收件地址用 ${VAR} 占位，
# 由本脚本在启动前渲染成一份实际配置再交给 alertmanager。
#
# fail-closed 是这里的核心：占位符缺失或为空时**非零退出、不启动 Alertmanager**。
# 反过来（带空地址启动）会得到一个"看起来正常、实际永远发不出告警"的组件 ——
# 比起不来危险得多，因为这正是审计 P1-6 描述的静默失联状态。
#
# 环境变量:
#   ALERTMANAGER_CONFIG_TEMPLATE  模板路径 (默认 /etc/alertmanager/alertmanager.yml)
#   ALERTMANAGER_RENDERED_CONFIG  渲染产物 (默认 /etc/alertmanager/rendered.yml)
#   ALERTMANAGER_BIN              可执行文件 (默认 /bin/alertmanager)
#   其余 ${...} 占位符直接取自环境，必须非空。

set -eu

TEMPLATE="${ALERTMANAGER_CONFIG_TEMPLATE:-/etc/alertmanager/alertmanager.yml}"
RENDERED="${ALERTMANAGER_RENDERED_CONFIG:-/etc/alertmanager/rendered.yml}"
BIN="${ALERTMANAGER_BIN:-/bin/alertmanager}"

log() { echo "[alertmanager-entrypoint] $*"; }
die() { echo "[alertmanager-entrypoint] ERROR: $*" >&2; exit 1; }

[ -f "$TEMPLATE" ] || die "配置模板不存在: $TEMPLATE"
[ -x "$BIN" ] || [ -n "$ALERTMANAGER_BIN" ] || die "找不到可执行的 Alertmanager: $BIN"

# ── 1. 收集模板里出现的占位符 ─────────────────────────────────────────────
# 用 sed 而非 `grep -o`：BusyBox 的 grep 不保证支持 -o（镜像里只有 /bin/sed）。
PLACEHOLDERS=$(sed -n 's/[^$]*\${\([A-Z0-9_]*\)}.*/\1/p' "$TEMPLATE" | sort -u)

[ -n "$PLACEHOLDERS" ] || die "模板 $TEMPLATE 里没有任何 \${VAR} 占位符，疑似放错了文件"

# ── 2. 校验每个占位符在环境中非空 ───────────────────────────────────────
for name in $PLACEHOLDERS; do
    eval "value=\${$name-}"
    [ -n "$value" ] || die "占位符 $name 在环境中缺失或为空；拒绝以'无人接收'的状态启动 Alertmanager"
    log "占位符 $name 已解析 (长度 ${#value})"
done

# ── 3. 渲染 ────────────────────────────────────────────────────────────
# 逐个占位符串行替换：每轮读上一轮的产物写下一轮（不能每轮都从 TEMPLATE 重读，
# 否则会把模板累积写 N 份到输出里）。值里的 / & \ | 需转义，否则会被二次解释。
cur="$TEMPLATE"
nxt="$RENDERED.tmp.$$"
for name in $PLACEHOLDERS; do
    eval "value=\${$name-}"
    esc=$(printf '%s' "$value" | sed 's/[\\&|]/\\&/g')
    sed "s|\${$name}|$esc|g" "$cur" > "$nxt"
    cur="$nxt"
done

if grep -q '\${' "$cur"; then
    leftover=$(grep -o '\${[A-Za-z0-9_]*}' "$cur" | sort -u | tr '\n' ' ')
    rm -f "$nxt"
    die "渲染后仍有未替换的占位符: $leftover"
fi

mv "$cur" "$RENDERED"
chmod 0600 "$RENDERED" 2>/dev/null || true
log "已渲染 $TEMPLATE -> $RENDERED"

# ── 4. 启动 ────────────────────────────────────────────────────────────
# 注意: 渲染产物可能含 webhook 密钥，不打印内容，只打印路径。
log "启动 Alertmanager"
exec "$BIN" \
    --config.file="$RENDERED" \
    --storage.path=/alertmanager \
    --web.external-url=http://localhost:9093
