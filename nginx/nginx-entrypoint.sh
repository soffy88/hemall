#!/bin/sh
# nginx 容器入口 (audit P1-7)
#
# 修掉审计 P1-7 的三件事：
#   1. 证书/私钥写死成仓库里不存在的绝对路径 → compose 把 ./ssl 建成空目录 →
#      nginx `[emerg] cannot open certificate` 反复重启。这里改为从挂载路径
#      读，并在渲染前逐项校验"存在 + 非空 + 可读"，缺就报一条说清补救办法的错。
#   2. `server_name api.hemall.example.com` 是占位域名却被 prod compose 当真
#      配置用。改为必填 HEMALL_API_SERVER_NAME，缺失即拒绝启动。
#   3. 启动路径上没有 `nginx -t` 门禁 —— 配置错的表征是 CrashLoop + 一屏
#      emerg。这里在 exec nginx 之前先 `nginx -t`，失败即非零退出。
#
# 渲染只替换 HEMALL_ 前缀的占位符：nginx 自己的 $host / $remote_addr /
# $proxy_add_x_forwarded_for 等必须原样保留，否则生成的配置是坏的。
#
# 环境变量:
#   HEMALL_API_SERVER_NAME  入口域名 (必填)
#   HEMALL_SSL_CERT_PATH    fullchain 证书路径 (必填，须存在且非空)
#   HEMALL_SSL_KEY_PATH     私钥路径 (必填，须存在且非空)
#   HEMALL_SSL_SOURCE_DIR   证书来源目录，仅用于错误提示 (必填)
#   NGINX_CONF_TEMPLATE     模板路径 (默认 /etc/nginx/nginx.conf.template)
#   NGINX_CONF_OUTPUT       渲染产物 (默认 /etc/nginx/nginx.conf)
#   NGINX_BIN               nginx 可执行文件 (默认 /usr/sbin/nginx)

set -eu

TEMPLATE="${NGINX_CONF_TEMPLATE:-/etc/nginx/nginx.conf.template}"
OUTPUT="${NGINX_CONF_OUTPUT:-/etc/nginx/nginx.conf}"
NGINX="${NGINX_BIN:-/usr/sbin/nginx}"
SSL_DIR="${HEMALL_SSL_SOURCE_DIR:-/etc/nginx/ssl}"

log() { echo "[nginx-entrypoint] $*"; }
die() { echo "[nginx-entrypoint] ERROR: $*" >&2; exit 1; }

# ── 1. 必填项 ──────────────────────────────────────────────────────────
[ -n "${HEMALL_API_SERVER_NAME:-}" ] || die \
"HEMALL_API_SERVER_NAME 未设置。
 入口域名必须与证书的 CN/SAN 一致，不能用占位域名。
 签发示例: certbot certonly --nginx -d <你的域名>"

_host_lc=$(printf '%s' "$HEMALL_API_SERVER_NAME" | tr 'A-Z' 'a-z')
case "$_host_lc" in
    *example*|*change_me*|*changeme*|*your-domain*|*yourdomain*|*your_domain*)
        die "HEMALL_API_SERVER_NAME='$HEMALL_API_SERVER_NAME' 仍是占位域名，请填真实域名
  (占位入口会让证书与 DNS 配置全部对不上，且是 P1-7/P3-9 修掉的原始问题)。"
        ;;
esac

[ -n "${HEMALL_SSL_CERT_PATH:-}" ] || die "HEMALL_SSL_CERT_PATH 未设置 (fullchain 证书路径)"
[ -n "${HEMALL_SSL_KEY_PATH:-}" ]  || die "HEMALL_SSL_KEY_PATH 未设置 (私钥路径)"

# ── 2. 证书材料校验 ────────────────────────────────────────────────────
[ -d "$SSL_DIR" ] || die \
"证书目录 '$SSL_DIR' 不存在或不是目录。
 把真实证书挂载进去，例如:
   mkdir -p ssl && certbot certonly --nginx -d <你的域名> --cert-path ssl --key-path ssl
 期望文件: \$HEMALL_SSL_CERT_PATH (fullchain) 与 \$HEMALL_SSL_KEY_PATH (privkey)。
 仓库刻意不提交证书 (*.pem 已在 .gitignore)。"

check_file() {
    _path="$1"
    _label="$2"
    [ -e "$_path" ] || die \
"缺少 $_label: '$_path'
 证书目录 '$SSL_DIR' 已挂载但文件不存在。
 请确认挂载源目录里有 fullchain.pem / privkey.pem (或自定义文件名后同步改
 HEMALL_SSL_CERT_PATH / HEMALL_SSL_KEY_PATH)。
 补齐方式: certbot certonly --nginx -d <你的域名> --cert-path $SSL_DIR --key-path $SSL_DIR"
    [ -f "$_path" ] || die "$_label '$_path' 不是普通文件"
    [ -s "$_path" ] || die "$_label '$_path' 是空文件 —— 挂载到了目录或下载中断，请重新签发"
    [ -r "$_path" ] || die "$_label '$_path' 不可读 (权限?容器以非 root 运行?)"
}

check_file "$HEMALL_SSL_CERT_PATH" "TLS 证书 (fullchain)"
check_file "$HEMALL_SSL_KEY_PATH" "TLS 私钥 (privkey)"

# 私钥权限过宽时给出提示 (不阻断: 证书/私钥常由宿主以 0644 挂进来)
key_mode=$(ls -l "$HEMALL_SSL_KEY_PATH" | cut -c1-10)
case "$key_mode" in
    -rwxr-xr-x|-rw-rw-rw-|-rw-rw-r--)
        log "WARNING: 私钥 $HEMALL_SSL_KEY_PATH 权限为 $key_mode，建议 chmod 600"
        ;;
esac

[ -f "$TEMPLATE" ] || die "配置模板不存在: $TEMPLATE"

# ── 3. 渲染 (只替换 HEMALL_ 前缀) ─────────────────────────────────────
# envsubst 本身**不认识** NGINX_ENVSUBST_FILTER (那是 nginx 镜像 entrypoint
# 的约定)，直接 `envsubst < tpl` 会把 nginx 自己的 $host / $remote_addr /
# $proxy_add_x_forwarded_for 一并替换成空串，生成一份坏配置。所以显式列出
# 要替换的变量名清单。
PLACEHOLDERS=$(sed -n 's/.*${\(HEMALL_[A-Z0-9_]*\)}.*/\1/p' "$TEMPLATE" | sort -u)
[ -n "$PLACEHOLDERS" ] || die "模板 $TEMPLATE 里没有 HEMALL_ 占位符，疑似挂错文件"

subst_list=""
for name in $PLACEHOLDERS; do
    eval "value=\${$name-}"
    [ -n "$value" ] || die "占位符 $name 在环境中缺失或为空"
    subst_list="$subst_list \${$name}"
done

# shellcheck disable=SC2086 # subst_list 必须作为单个参数展开
envsubst "$subst_list" < "$TEMPLATE" > "$OUTPUT"

if grep -q '\${HEMALL_' "$OUTPUT"; then
    leftover=$(grep -o '\${HEMALL_[A-Za-z0-9_]*}' "$OUTPUT" | sort -u | tr '\n' ' ')
    die "渲染后仍有未替换的占位符: $leftover"
fi
# 反向检查: nginx 自己的变量必须还在，被误替换的话生成的配置是坏的
for builtin in host remote_addr proxy_add_x_forwarded_for scheme request_uri; do
    if grep -q "\$$builtin" "$TEMPLATE" && ! grep -q "\$$builtin" "$OUTPUT"; then
        die "渲染吞掉了 nginx 自身的 \$$builtin 变量，生成的配置无效"
    fi
done
log "已渲染 $TEMPLATE -> $OUTPUT (server_name=$HEMALL_API_SERVER_NAME)"

# ── 4. nginx -t 门禁 ──────────────────────────────────────────────────
if ! "$NGINX" -t -c "$OUTPUT"; then
    die "nginx -t 失败 —— 配置有错，未启动 nginx (避免 CrashLoop 刷屏)。
 请按上面的 emerg 行修正；模板在 $TEMPLATE。"
fi
log "nginx -t 通过"

# ── 5. 启动 ───────────────────────────────────────────────────────────
log "启动 nginx (server_name=$HEMALL_API_SERVER_NAME)"
exec "$NGINX" -c "$OUTPUT" -g 'daemon off;'
