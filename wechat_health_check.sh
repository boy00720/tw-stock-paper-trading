#!/bin/bash
# 微信 Bridge 健康檢查與人工刷新提示腳本
# 建議每 10 分鐘執行一次：crontab -e 加入
# */10 * * * * /Users/w/Cursor/automation/wechat_health_check.sh >> /Users/w/Cursor/automation/logs/health_check.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

HEALTH_URL="http://127.0.0.1:3001/health"
ALERT_USER="${WECHAT_ALERT_USER:-}"  # 設定環境變數指定要通知的微信用戶 ID
BRIDGE_LABEL="com.w.wechat-bridge-3001"
API_LABEL="com.w.wechat-api-3003"
LAUNCHD_DOMAIN="gui/$(id -u)"

SELFTEST_SCRIPT="$SCRIPT_DIR/scripts/wechat_bridge_selftest.py"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

launchd_loaded() {
    local label="$1"
    launchctl list | grep -q "$label"
}

kickstart_label() {
    local label="$1"
    launchctl kickstart -k "$LAUNCHD_DOMAIN/$label" >/dev/null 2>&1
}

render_summary() {
    HEALTH_JSON="$body" python3 - <<'PY'
import json
import os
from datetime import datetime, timezone

body = os.environ.get("HEALTH_JSON", "")
try:
    data = json.loads(body)
except Exception:
    print("❌ 無法解析 /health JSON")
    raise

def fmt_age(seconds):
    if seconds is None:
        return "未知"
    try:
        seconds = int(seconds)
    except Exception:
        return str(seconds)
    if seconds < 60:
        return f"{seconds} 秒前"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒前"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小時 {minutes} 分前"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小時前"

def fmt_time(value):
    if not value:
        return "無"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)

def line(title, value):
    print(f"- {title}：{value}")

ready = data.get("ready_for_push")
pending = data.get("pending_failed_count")
context_age = fmt_age(data.get("context_age_seconds"))
summary_text = data.get("summary_text") or "無"
summary_status = data.get("summary_status") or "unknown"
recommended_action = data.get("recommended_action") or "目前不需要額外操作"
action_required = data.get("action_required")
runbook_steps = data.get("runbook_steps") or []

status_map = {
    "ready": "正常，可直接推送",
    "repair_available": "可直接推送，但有待補發",
    "waiting_for_context_refresh": "待人工刷新後補發",
    "not_ready": "未就緒，需要刷新 context",
    "missing_target_user": "未設定目標用戶",
}

print("微信 Bridge 狀態摘要")
print(f"- 目前狀態：{status_map.get(summary_status, '狀態未明')}" )
line("建議操作", recommended_action)
line("是否需要立即處理", "是" if action_required else "否")
line("判讀摘要", summary_text)
line("目標用戶", data.get("target_user") or "未設定")
line("待補發數量", pending)
line("目前 context 狀態", "有效" if not data.get("context_invalid") else "失效")
line("目前 context 來源", data.get("context_source") or "未知")
line("目前 context 原因", data.get("context_reason") or "未知")
line("目前 context 新鮮度", context_age)

last_refresh = data.get("last_context_refresh") or {}
print("- 最近一次 context 刷新：")
line("  時間", fmt_time(last_refresh.get("updated_at")))
line("  失效時間", fmt_time(last_refresh.get("invalidated_at")))
line("  失效原因", last_refresh.get("invalid_reason") or "無")

direct = data.get("latest_direct_success") or {}
print("- 最近一次直接送達成功：")
line("  時間", fmt_time(direct.get("time")))
line("  trace_id", direct.get("trace_id") or "無")
line("  長度", f"{direct.get('text_len')} 字" if direct.get("text_len") is not None else "無")
line("  分段數", direct.get("chunk_count") if direct.get("chunk_count") is not None else "無")
line("  ack_mode", direct.get("ack_mode") or "無")

replay = data.get("latest_replayed_success") or {}
print("- 最近一次補發成功：")
line("  時間", fmt_time(replay.get("time")))
line("  queue_id", replay.get("queue_id") or "無")
line("  原始 trace", replay.get("original_trace_id") or "無")
line("  觸發來源", replay.get("trigger_source") or "無")
line("  觸發文字", replay.get("trigger_text") or "無")
line("  長度", f"{replay.get('text_len')} 字" if replay.get("text_len") is not None else "無")
line("  分段數", replay.get("chunk_count") if replay.get("chunk_count") is not None else "無")
line("  ack_mode", replay.get("ack_mode") or "無")

print("- 處理指引：")
if runbook_steps:
    for step in runbook_steps:
        print(f"  - {step}")
else:
    print("  - 無")

if ready:
    print("- 下一步：目前可以直接繼續推送或維持觀察。")
elif "再補發" in recommended_action:
    print("- 下一步：先到微信回覆 ping，等 context 刷新後，再重新執行健康檢查或補發。")
elif "補發佇列" in recommended_action:
    print("- 下一步：直接執行 repair，清掉目前待補發佇列。")
elif "設定推送目標" in recommended_action:
    print("- 下一步：先設定目標用戶，再重新執行健康檢查。")
else:
    print("- 下一步：先到微信回覆 ping，再重新執行健康檢查確認狀態。")
PY
}

# 檢查 bridge 是否回應
check_health() {
    local quiet="${1:-false}"
    local response
    local http_code
    response=$(curl -s -w "\n%{http_code}" "$HEALTH_URL" 2>/dev/null || echo -e "\n000")
    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')

    if [ "$http_code" != "200" ]; then
        log "❌ Health check 失敗: HTTP $http_code"
        return 1
    fi

    ready=$(echo "$body" | grep -o '"ready_for_push":[a-z]*' | cut -d: -f2 || echo "false")
    replay_ready=$(echo "$body" | grep -o '"replay_ready":[a-z]*' | cut -d: -f2 || echo "false")
    requires_user_refresh=$(echo "$body" | grep -o '"requires_user_refresh":[a-z]*' | cut -d: -f2 || echo "false")
    context_reason=$(echo "$body" | grep -o '"context_reason":"[^"]*"' | cut -d'"' -f4 || echo "unknown")
    context_invalid_reason=$(echo "$body" | grep -o '"context_invalid_reason":"[^"]*"' | cut -d'"' -f4 || echo "")
    context_age=$(echo "$body" | grep -o '"context_age_seconds":[0-9]*' | cut -d: -f2 || echo "0")
    pending_count=$(echo "$body" | grep -o '"pending_failed_count":[0-9]*' | cut -d: -f2 || echo "0")

    if [ "$quiet" != "true" ]; then
        log "ℹ️  狀態: ready=$ready, replay_ready=$replay_ready, prompt_refresh=$requires_user_refresh, reason=$context_reason, invalid_reason=${context_invalid_reason:-none}, age=${context_age}s, pending=$pending_count"
        render_summary
    fi

    if [ "$ready" != "true" ]; then
        if [ "$quiet" != "true" ]; then
            log "⚠️  Context 未就緒 (reason: $context_reason, invalid_reason: ${context_invalid_reason:-none}, age: ${context_age}s)"
        fi
        return 2
    fi

    return 0
}

# 嘗試補發佇列；這不是取得新 token 的手段
try_repair() {
    local target="${1:-}"
    if [ -z "$target" ]; then
        log "❌ 未指定修復目標用戶，跳過 repair"
        return 1
    fi

    log "🔧 嘗試補發待送佇列: target=$target"
    response=$(curl -s -X POST "http://127.0.0.1:3001/repair" \
        -H "Content-Type: application/json" \
        -d "{\"to_user\":\"$target\",\"trigger_source\":\"health_check\"}" 2>/dev/null || echo '{"status":"error"}')

    if echo "$response" | grep -q '"ready":true'; then
        log "✅ 補發請求已受理: $response"
        return 0
    else
        log "⚠️ 補發未就緒，通常代表仍需使用者在微信發 ping 刷新 context_token: $response"
        return 1
    fi
}

wait_for_context_ready() {
    local attempts="${1:-5}"
    local sleep_seconds="${2:-2}"
    local i=1
    while [ $i -le "$attempts" ]; do
        check_health true || true
        if [ "${ready:-false}" = "true" ]; then
            log "✅ context 已恢復可推播（第 ${i} 次檢查）"
            return 0
        fi
        sleep "$sleep_seconds"
        i=$((i + 1))
    done
    log "⚠️ 等待 ${attempts} 次後 context 仍未就緒"
    return 1
}

try_repair_until_stable() {
    local target="${1:-}"
    local max_rounds="${2:-4}"
    local round=1
    local previous_pending="-1"

    while [ $round -le "$max_rounds" ]; do
        try_repair "$target" || return 1
        sleep 1
        check_health true || true
        local current_pending="${pending_count:-0}"
        log "📉 repair round ${round}: pending=${current_pending}"

        if [ "$current_pending" = "0" ]; then
            return 0
        fi

        if [ "$previous_pending" = "$current_pending" ]; then
            log "⚠️ pending 未下降（${current_pending}），停止連續補發"
            return 2
        fi

        previous_pending="$current_pending"
        round=$((round + 1))
    done

    return 2
}

build_notify_text() {
    HEALTH_JSON="$body" python3 - <<'PY'
import json
import os

body = os.environ.get("HEALTH_JSON", "")
try:
    data = json.loads(body)
except Exception:
    print("⚠️ 微信 Bridge 目前缺少有效對話上下文，請回覆「ping」以恢復推播功能。")
    raise SystemExit(0)

recommended_action = data.get("recommended_action") or "先到微信回覆 ping 刷新推播上下文"
runbook_steps = data.get("runbook_steps") or []
summary_text = data.get("summary_text") or "微信 Bridge 目前缺少有效對話上下文。"

lines = [
    "⚠️ 微信 Bridge 提醒",
    summary_text,
    f"建議操作：{recommended_action}",
]

for step in runbook_steps[:3]:
    clean = str(step).replace("第 1 層：", "1.").replace("第 2 層：", "2.").replace("第 3 層：", "3.")
    lines.append(clean)

print("\n".join(lines))
PY
}

notify_user() {
    local target="${1:-}"
    if [ -z "$target" ]; then
        return
    fi

    local notify_text
    local payload
    notify_text="$(build_notify_text)"
    payload="$(TARGET="$target" TEXT="$notify_text" python3 - <<'PY'
import json, os
print(json.dumps({
    'to_user': os.environ['TARGET'],
    'text': os.environ['TEXT'],
}, ensure_ascii=False))
PY
)"

    log "📱 提醒用戶手動刷新 context_token: $target"
    curl -s -X POST "http://127.0.0.1:3001/send" \
        -H "Content-Type: application/json" \
        -d "$payload" > /dev/null 2>&1 || true
}

print_runbook() {
    local scenario="$1"
    echo "處理指引"

    case "$scenario" in
        bridge_down)
            echo "- 第 1 層：先確認 bridge 服務是否存活。"
            echo "  - 可直接執行：launchctl list | grep wechat-bridge-3001"
            echo "- 第 2 層：若服務不存在，重新拉起 bridge / API。"
            echo "  - 已自動嘗試：launchctl kickstart -k gui/$(id -u)/$BRIDGE_LABEL"
            echo "  - 若仍失敗：bash $SCRIPT_DIR/start_wechat.sh"
            echo "- 第 3 層：服務恢復後，重新執行健康檢查。"
            echo "  - 指令：bash $SCRIPT_DIR/wechat_health_check.sh"
            ;;
        context_not_ready)
            echo "- 第 1 層：先看上方「建議操作」與「下一步」。"
            echo "- 第 2 層：若提示要刷新 context，就到微信對 bot 回覆 ping。"
            echo "- 第 3 層：若有待補發佇列，刷新後再重跑健康檢查，讓腳本自動驗證 queue→replay。"
            echo "  - 指令：bash $SCRIPT_DIR/wechat_health_check.sh"
            ;;
        repair_still_blocked)
            echo "- 第 1 層：代表目前 repair 仍無法完成，通常是 context 尚未刷新。"
            echo "- 第 2 層：先到微信回覆 ping，等待 5 到 10 秒。"
            echo "- 第 3 層：重新執行健康檢查，確認待補發佇列是否清為 0。"
            echo "  - 指令：bash $SCRIPT_DIR/wechat_health_check.sh"
            ;;
        missing_target_user)
            echo "- 第 1 層：先設定 WECHAT_ALERT_USER 或 bridge 預設 target_user。"
            echo "- 第 2 層：確認 /health 已能看到 target_user。"
            echo "  - 指令：curl -s $HEALTH_URL"
            echo "- 第 3 層：再重新執行健康檢查。"
            ;;
        *)
            echo "- 先重新執行健康檢查，依最新摘要與自測結果處理。"
            ;;
    esac
}

run_selftest() {
    if [ ! -f "$SELFTEST_SCRIPT" ]; then
        log "⚠️ 找不到自測腳本，跳過一鍵自測: $SELFTEST_SCRIPT"
        return 0
    fi

    log "🧪 開始執行微信 Bridge 一鍵自測"
    python3 "$SELFTEST_SCRIPT" || log "⚠️ 一鍵自測回傳非 0，請檢查上方輸出"
}

# 重啟服務
restart_service() {
    log "🔄 嘗試重啟服務..."

    if launchd_loaded "$BRIDGE_LABEL" || launchd_loaded "$API_LABEL"; then
        log "   偵測到 launchd 常駐管理，改用 launchctl kickstart"
        if launchd_loaded "$BRIDGE_LABEL"; then
            kickstart_label "$BRIDGE_LABEL" && log "   已重啟 $BRIDGE_LABEL"
        fi
        if launchd_loaded "$API_LABEL"; then
            kickstart_label "$API_LABEL" && log "   已重啟 $API_LABEL"
        fi
        sleep 5
        return
    fi

    log "   未偵測到 launchd 管理，跳過自動重啟，請手動執行 start_wechat.sh"
}

# 主邏輯
main() {
    log "=== 健康檢查開始 ==="

    if ! curl -s "$HEALTH_URL" > /dev/null 2>&1; then
        log "❌ Bridge 無回應，服務可能已停止"
        print_runbook "bridge_down"
        restart_service
        exit 1
    fi

    check_result=0
    check_health || check_result=$?

    if [ $check_result -eq 0 ]; then
        run_selftest
        log "✅ 健康檢查通過"
        exit 0
    fi

    before_pending="${pending_count:-0}"
    before_ready="${ready:-false}"

    if [ $check_result -eq 2 ] && [ -n "$ALERT_USER" ]; then
        print_runbook "context_not_ready"

        log "ℹ️ 健康檢查不再使用 outbound ping 當作 context 刷新手段；請先在微信手動回覆 ping。"
        wait_for_context_ready 6 2 || true

        if [ "${ready:-false}" = "true" ] || check_health true; then
            try_repair_until_stable "$ALERT_USER" 5 || true
            sleep 1
            check_health || true

            after_pending="${pending_count:-0}"
            latest_replay_queue=$(echo "$body" | grep -o '"queue_id":"[^"]*"' | head -n1 | cut -d'"' -f4 || true)
            latest_replay_time=$(echo "$body" | grep -o '"time":"[^"]*"' | head -n1 | cut -d'"' -f4 || true)

            direct_send_ready="${ready:-false}"
            backlog_drained="false"
            if [ "$after_pending" = "0" ]; then
                backlog_drained="true"
            fi

            log "📈 補發前後對比: pending ${before_pending} -> ${after_pending}"
            log "🧭 狀態: direct_send_ready=${direct_send_ready}, backlog_drained=${backlog_drained}"
            if [ -n "${latest_replay_queue:-}" ]; then
                log "🧾 最近 replay 成功: queue_id=${latest_replay_queue}, time=${latest_replay_time:-unknown}"
            else
                log "🧾 最近 replay 成功: 尚無可用 queue_id（可能尚未觸發或尚未成功）"
            fi

            if [ "$after_pending" = "0" ]; then
                run_selftest
                log "✅ 補發排空完成，健康檢查通過"
                exit 0
            fi

            if [ "${ready:-false}" != "true" ]; then
                log "⚠️ 分類建議：context 仍未刷新，請先到微信回覆 ping 後再補發"
            elif [ -z "${latest_replay_queue:-}" ]; then
                log "⚠️ 分類建議：replay 尚未觸發，請稍後重跑健康檢查或手動 /repair"
            else
                log "⚠️ 分類建議：replay 已觸發但仍有殘留佇列，請檢查 replay 失敗明細"
            fi
        fi

        log "⚠️ 補發後仍有殘留，提醒使用者手動刷新後重試"
        print_runbook "repair_still_blocked"
        notify_user "$ALERT_USER"
    fi

    if [ $check_result -ne 0 ] && [ -z "$ALERT_USER" ]; then
        print_runbook "missing_target_user"
    fi

    log "=== 健康檢查結束 ==="
}

main "$@"
