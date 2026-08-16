#!/usr/bin/env bash
# hemal-monitor runner — 后台运行、断网自动重试接续（同一 session-id，记忆不丢）
# 用法:
#   run-monitor.sh daily|weekly|weekly_review [--foreground]
set -uo pipefail

PROJECT=/home/soffy/projects/hemal
VEYA="$PROJECT/.veya-project"
SESSIONS="$VEYA/sessions"
OUT="$VEYA/output"
PROMPT="$VEYA/agent-prompt.md"
LOCK="$VEYA/.monitor.lock"
TASK="${1:-daily}"
MODE="${2:-}"

cd "$PROJECT" || exit 2
mkdir -p "$SESSIONS" "$OUT" "$OUT/incidents"

# 并发控制：同一时刻只允许一个监控任务（SCHEDULE.md concurrency: 1）
exec 9>"$LOCK"
flock -n 9 || { echo "[$(date -u +%FT%TZ)] 另一个监控任务正在运行，本次跳过" >> "$OUT/runner.log"; exit 0; }

echo "[$(date -u +%FT%TZ)] start task=$TASK" >> "$OUT/runner.log"

run_once() {
  local attempt="$1"
  local prompt_text
  prompt_text="$(cat "$PROMPT")"
  PI_OFFLINE=1 timeout 3600 pi -p \
    --session-id hemal-monitor \
    --session-dir "$SESSIONS" \
    --name "hemal-monitor" \
    --append-system-prompt "$prompt_text" \
    "执行 $TASK 监控任务。按 agent-prompt.md 的铁律与任务清单执行，结果写入 .veya-project/output/ 并更新 PROJECT_STATE.md。当前是第 $attempt 次尝试。"
}

attempt=0
while :; do
  attempt=$((attempt + 1))
  echo "[$(date -u +%FT%TZ)] attempt=$attempt task=$TASK" >> "$OUT/runner.log"
  run_once "$attempt" >> "$OUT/runner.log" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "[$(date -u +%FT%TZ)] done task=$TASK rc=0" >> "$OUT/runner.log"
    exit 0
  fi
  # 非零退出（网络错误/超时/服务端错误）→ 指数退避后重试；同一 session-id 自动接续记忆
  wait_s=$(( 15 * 2 ** (attempt - 1) > 300 ? 300 : 15 * 2 ** (attempt - 1) ))
  echo "[$(date -u +%FT%TZ)] rc=$rc (疑似网络中断)，${wait_s}s 后重试 (第 $((attempt+1)) 次)" >> "$OUT/runner.log"
  [ "$attempt" -ge 6 ] && { echo "[$(date -u +%FT%TZ)] give up task=$TASK rc=$rc" >> "$OUT/runner.log"; exit "$rc"; }
  sleep "$wait_s"
done
