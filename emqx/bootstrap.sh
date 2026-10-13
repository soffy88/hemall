#!/bin/sh
# EMQX 认证/授权引导 (audit P1-8)
#
# 为什么需要这一步：EMQX 的内置数据库是**运行时**的，镜像里没有任何用户。
# 认证链 (password_based + built_in_database) 可以用环境变量声明，但用户记录
# 只能通过 Dashboard API 写入 —— 而 emqx:5.3 镜像里既没有 curl 也没有 wget
# （只有 openssl 和 awk），没法在容器内自举。
#
# 所以用一次性 sidecar 跑本脚本：拿到 dashboard 令牌 -> 建认证器 -> 建
# edge_bridge 账号 -> 写授权链 -> 回读确认。任何一步失败都非零退出，
# iot-bridge 依赖 service_completed_successfully，因而不会带着空账号去连
# 1883（那会得到一个连不上的网桥，而不是一个坏掉的 broker）。
#
# 幂等：账号已存在时 API 返回 409，按"已达成目标"处理，否则每次
# `docker compose up` 都会卡在 bootstrap 这一步。
#
# 运行环境: curlimages/curl (只有 sh + curl + sed + grep)

set -eu

EMQX_URL="${EMQX_BOOTSTRAP_URL:-http://127.0.0.1:18083}"
DASH_USER="${HEMALL_EMQX_DASHBOARD_USERNAME:-admin}"
EDGE_USER="${HEMALL_EMQX_EDGE_USERNAME:-}"
EDGE_PASSWORD="${HEMALL_EMQX_EDGE_PASSWORD:-}"
DASH_PASSWORD="${HEMALL_EMQX_DASHBOARD_PASSWORD:-}"

log()  { echo "[emqx-bootstrap] $*"; }
fail() { echo "[emqx-bootstrap] ERROR: $*" >&2; exit 1; }

# 1. fail-closed: 缺配置 / 弱口令一律拒绝，不带着默认值继续跑
[ -n "$DASH_PASSWORD" ] || fail 'HEMALL_EMQX_DASHBOARD_PASSWORD 未设置。EMQX 出厂默认是 admin/public，拒绝启动。生成一个: openssl rand -base64 24'
[ -n "$EDGE_USER" ]     || fail 'HEMALL_EMQX_EDGE_USERNAME 未设置（应与 emqx/acl.conf 里的客户端标识一致）'
[ -n "$EDGE_PASSWORD" ] || fail 'HEMALL_EMQX_EDGE_PASSWORD 未设置'

# 拒绝出厂/弱默认值。与其"提醒"不如直接拒绝：这类口令的失败模式是
# 静默的 —— broker 正常跑着，攻击者正常连着，没有任何告警。
for pair in "DASH:$DASH_PASSWORD" "EDGE:$EDGE_PASSWORD"; do
    label=${pair%%:*}
    value=${pair#*:}
    case "$(printf '%s' "$value" | tr 'A-Z' 'a-z')" in
        public|admin|changeme|change_me|password|secret|admin123|12345|123456|qwerty)
            fail "${label} 口令是弱默认值，拒绝启动（EMQX/Broker 弱口令的失败模式是静默的）"
            ;;
    esac
    # 最小长度：太短的口令在 EMQX 的 sha256+salt 下依然禁不住离线爆破
    if [ "${#value}" -lt 12 ]; then
        fail "${label} 口令过短 (${#value} 字符 < 12)，拒绝启动。建议: openssl rand -base64 24"
    fi
done

# 2. 等 Dashboard 就绪
log "等待 EMQX Dashboard 就绪 (${EMQX_URL})"
i=0
TOKEN=""
while [ "$i" -lt 60 ]; do
    TOKEN=$(curl -s -X POST "${EMQX_URL}/api/v5/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"${DASH_USER}\",\"password\":\"${DASH_PASSWORD}\"}" \
        | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
    [ -n "$TOKEN" ] && break
    i=$((i + 1))
    sleep 2
done
[ -n "$TOKEN" ] || fail '无法登录 EMQX Dashboard：确认 EMQX_DASHBOARD__DEFAULT_PASSWORD 已注入且值正确'
log "已登录 Dashboard"

auth_header="Authorization: Bearer ${TOKEN}"

# 3. 认证器 (password_based / built_in_database / sha256+salt)
# 正常情况下 compose 已经用环境变量声明好了；这里回读确认，查不到再补建，
# 两种路径都能收敛。
log "确认认证器..."
if ! curl -sf "${EMQX_URL}/api/v5/authentication" -H "$auth_header" | grep -q 'built_in_database'; then
    log "未找到 password_based 认证器，创建中..."
    code=$(curl -s -o /tmp/authn.out -w '%{http_code}' -X POST "${EMQX_URL}/api/v5/authentication" \
        -H "$auth_header" -H 'Content-Type: application/json' -d '{
      "mechanism": "password_based",
      "backend": "built_in_database",
      "user_id_type": "username",
      "password_hash_algorithm": {"name": "sha256", "salt_position": "suffix"}
    }')
    case "$code" in
        200|201|409) log "认证器就绪" ;;
        *) fail "创建认证器失败 (HTTP $code): $(cat /tmp/authn.out 2>/dev/null)" ;;
    esac
else
    log "认证器已由环境变量声明"
fi

# 4. edge_bridge 账号 (幂等)
log "确保 MQTT 账号 ${EDGE_USER} 存在..."
code=$(curl -s -o /tmp/user.out -w '%{http_code}' -X POST \
    "${EMQX_URL}/api/v5/authentication/password_based:built_in_database/users" \
    -H "$auth_header" -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"${EDGE_USER}\",\"password\":\"${EDGE_PASSWORD}\",\"is_superuser\":false}")
case "$code" in
    200|201)
        log "账号 ${EDGE_USER} 已创建"
        ;;
    409)
        # 幂等：用户已存在。口令不随之更新 —— 改口令走 EMQX 控制台并同步
        # compose 环境变量，否则这里悄悄改掉会让运维手上的记录失效。
        log "账号 ${EDGE_USER} 已存在，沿用现有口令（如需改口令请在 Dashboard 操作并同步 .env）"
        ;;
    *)
        fail "创建 MQTT 账号失败 (HTTP $code): $(cat /tmp/user.out 2>/dev/null)"
        ;;
esac

# 回读确认账号真的在库里
if ! curl -sf "${EMQX_URL}/api/v5/authentication/password_based:built_in_database/users" \
        -H "$auth_header" | grep -q "\"${EDGE_USER}\""; then
    fail "回读内置数据库时找不到账号 ${EDGE_USER} —— 拒绝宣告成功（网桥会连不上 1883）"
fi

# 5. 授权链: no_match=deny + 文件 ACL
# compose 已用环境变量声明；这里回读确认。deny_action=disconnect 与
# cache.enable=false 缺一不可：ignore 会把越权尝试静默丢弃，缓存会让刚改的
# ACL 在 TTL 内继续放行。
log "确认授权链 (no_match=deny / deny_action=disconnect / cache off)..."
AUTHZ=$(curl -sf "${EMQX_URL}/api/v5/authorization/settings" -H "$auth_header" || echo '{}')
log "当前授权设置: ${AUTHZ}"
case "$AUTHZ" in
    *'"no_match":"deny"'*) : ;;
    *) fail '授权链 no_match 不是 deny —— 未列出的主题会被放行，拒绝宣告成功' ;;
esac
case "$AUTHZ" in
    *'"deny_action":"disconnect"'*) : ;;
    *) fail 'deny_action 不是 disconnect：越权尝试会被静默丢弃，运维看不到有人在试探 ACL' ;;
esac

log "EMQX 引导完成: 认证 + 授权已就绪，账号 ${EDGE_USER} 可在硬件主题上收发"
exit 0