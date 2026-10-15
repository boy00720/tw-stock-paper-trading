import { ILinkClient, MessageItemType } from "weixin-ilink";
import fs from "fs";
import http from "http";
import crypto from "crypto";
import os from "os";
import path from "path";

const CURSOR_FILE = "logs/wechat_cursor.dat";
const CURSOR_DIR  = "logs";
const LOCAL_CONFIG_FILE = "wechat_config.json";
const DELIVERY_LOG_FILE = "logs/wechat_delivery.jsonl";
const LOCAL_CONTEXT_CACHE_FILE = "logs/wechat_context_cache.json";
const INBOUND_AUDIT_FILE = "logs/wechat_inbound.jsonl";
const FAILED_QUEUE_FILE = "logs/wechat_failed_queue.json";
const SHARED_STATE_DIR = process.env.WECHAT_SHARED_STATE_DIR || path.join(os.homedir(), ".weixin-claude-bot");
const SHARED_CREDENTIALS_FILE = path.join(SHARED_STATE_DIR, "credentials.json");
const SHARED_CONTEXT_CACHE_FILE = path.join(SHARED_STATE_DIR, "context-tokens.json");
const CHANNEL_VERSION = "weixin-ilink/0.1.0";
const SEND_TIMEOUT_MS = 15000;
const SEND_MAX_LENGTH = Math.max(500, Number.parseInt(process.env.WECHAT_SEND_MAX_LENGTH || "1000", 10) || 1000);
const SEND_CHUNK_DELAY_MS = Math.max(0, Number.parseInt(process.env.WECHAT_SEND_CHUNK_DELAY_MS || "1800", 10) || 1800);
const SEND_CHUNK_JITTER_MS = Math.max(0, Number.parseInt(process.env.WECHAT_SEND_CHUNK_JITTER_MS || "250", 10) || 250);
const SEND_BURST_LIMIT = Math.max(1, Number.parseInt(process.env.WECHAT_SEND_BURST_LIMIT || "3", 10) || 3);
const MAX_CONTEXT_USERS = 200;
const ENABLE_POLLING = process.env.WECHAT_BRIDGE_ENABLE_POLLING === "1";
const CONTEXT_MAX_AGE_HOURS = Number.parseFloat(process.env.WECHAT_CONTEXT_MAX_AGE_HOURS || "8");
const CONTEXT_MAX_AGE_MS = (
  Number.isFinite(CONTEXT_MAX_AGE_HOURS) && CONTEXT_MAX_AGE_HOURS > 0
    ? Math.round(CONTEXT_MAX_AGE_HOURS * 60 * 60 * 1000)
    : 8 * 60 * 60 * 1000
);
const FAILED_QUEUE_MAX_ITEMS = Math.max(1, Number.parseInt(process.env.WECHAT_FAILED_QUEUE_MAX_ITEMS || "50", 10) || 50);
const FAILED_QUEUE_MAX_AGE_HOURS = Math.max(1, Number.parseFloat(process.env.WECHAT_FAILED_QUEUE_MAX_AGE_HOURS || "24") || 24);
const FAILED_QUEUE_MAX_AGE_MS = Math.round(FAILED_QUEUE_MAX_AGE_HOURS * 60 * 60 * 1000);
const REPAIR_MAX_REPLAY = Math.max(1, Number.parseInt(process.env.WECHAT_REPAIR_MAX_REPLAY || "6", 10) || 6);
const INBOUND_DEDUPE_WINDOW_MS = Math.max(1000, Number.parseInt(process.env.WECHAT_INBOUND_DEDUPE_WINDOW_MS || "8000", 10) || 8000);
const CONTEXT_INVALID_NOTIFY_COOLDOWN_MS = Math.max(1000, Number.parseInt(process.env.WECHAT_CONTEXT_INVALID_NOTIFY_COOLDOWN_MS || "600000", 10) || 600000);
const inboundMessageCache = new Map();
const recentContextInvalidAlerts = new Map();
const activeRepairTargets = new Set();

// 定時自 ping 設定：預設每 4 小時發送一次、age ≥ 3 小時才觸發；env 可覆寫
const KEEP_ALIVE_INTERVAL_MS = Math.max(60_000,
  Number.parseInt(process.env.WECHAT_KEEPALIVE_INTERVAL_MS || String(4 * 60 * 60 * 1000), 10) || 4 * 60 * 60 * 1000);
const KEEP_ALIVE_MIN_AGE_MS = Math.max(60_000,
  Number.parseInt(process.env.WECHAT_KEEPALIVE_MIN_AGE_MS || String(3 * 60 * 60 * 1000), 10) || 3 * 60 * 60 * 1000);
const KEEP_ALIVE_TEXT = "\u200B"; // 零寬空格，用於觸發 token 刷新但用戶看不到

// 支援的指令與對應的 API 端點
const COMMAND_MAP = {
  "查籌碼":      { endpoint: "/query_stock",                    desc: "查個股報價與法人籌碼" },
  "晨報":        { endpoint: "/run/morning",                    desc: "台股晨間策略報告" },
  "盤後":        { endpoint: "/run/afternoon",                  desc: "盤後流程" },
  "今日新聞":    { endpoint: "/run/news",                       desc: "今日財經新聞摘要" },
  "日誌":        { endpoint: "/run/evening",                    desc: "生成今日日誌" },
  "電商週報":    { endpoint: "/run/ecommerce",                  desc: "跨境電商週報" },
  "本週反思":    { endpoint: "/run/reflect",                    desc: "本週反思報告" },
  "查服務狀態":  { endpoint: "/service/status",                 desc: "查詢服務狀態" },
  "服務狀態":    { endpoint: "/service/status",                 desc: "查詢服務狀態" },
  "服務列表":    { endpoint: "/service/status",                 desc: "查詢服務狀態" },
  "重啟飛書":    { endpoint: "/service/restart/飛書",           desc: "重啟飛書 Bot" },
  "重启飞书":    { endpoint: "/service/restart/飛書",           desc: "重啟飛書 Bot" },
  "重啟gateway": { endpoint: "/service/restart/gateway",        desc: "重啟 Claude Gateway" },
  "重启gateway": { endpoint: "/service/restart/gateway",        desc: "重啟 Claude Gateway" },
  "重啟proxy":   { endpoint: "/service/restart/proxy",          desc: "重啟 Nvidia Proxy" },
  "重启proxy":   { endpoint: "/service/restart/proxy",          desc: "重啟 Nvidia Proxy" },
  "重啟dsa":     { endpoint: "/service/restart/dsa",            desc: "重啟 DSA Server" },
  "重启dsa":     { endpoint: "/service/restart/dsa",            desc: "重啟 DSA Server" },
  "重啟微信bot": { endpoint: "/service/restart/微信bot",        desc: "重啟微信 Claude Bot" },
  "重启微信bot": { endpoint: "/service/restart/微信bot",        desc: "重啟微信 Claude Bot" },
  "重啟微信":    { endpoint: "/service/restart/微信",           desc: "重啟微信 Bridge（受保護）" },
  "重启微信":    { endpoint: "/service/restart/微信",           desc: "重啟微信 Bridge（受保護）" },
  "重啟微信api": { endpoint: "/service/restart/微信api",        desc: "重啟微信 API（受保護）" },
  "重启微信api": { endpoint: "/service/restart/微信api",        desc: "重啟微信 API（受保護）" },
};

const DEEP_QUERY_KEYWORDS = [
  "三大法人",
  "法人",
  "籌碼",
  "研究",
  "分析",
  "走勢",
  "台積電",
];


// 白名單權限驗證
function isUserAuthorized(userId) {
  try {
    const config = readJsonFile(LOCAL_CONFIG_FILE, "微信設定");
    if (!config || !config.enable_auth) {
      return true; // 未啟用權限驗證則允許所有用戶
    }
    const allowedList = config.allowed_wx_ids || [];
    return allowedList.includes(userId);
  } catch (e) {
    console.error("權限驗證失敗:", e.message);
    return true; // 驗證失敗時預設允許（避免鎖死）
  }
}

function ensureLogDir() {
  if (!fs.existsSync(CURSOR_DIR)) {
    fs.mkdirSync(CURSOR_DIR, { recursive: true });
  }
}

function ensureParentDir(file) {
  const dir = path.dirname(file);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

function resolveCredentialsLocation() {
  if (fs.existsSync(SHARED_CREDENTIALS_FILE)) {
    return { file: SHARED_CREDENTIALS_FILE, source: "shared" };
  }
  return { file: LOCAL_CONFIG_FILE, source: "local" };
}

function resolveContextCacheLocation() {
  if (fs.existsSync(SHARED_CREDENTIALS_FILE) || fs.existsSync(SHARED_CONTEXT_CACHE_FILE)) {
    return { file: SHARED_CONTEXT_CACHE_FILE, source: "shared" };
  }
  return { file: LOCAL_CONTEXT_CACHE_FILE, source: "local" };
}

function readJsonFile(file, label) {
  try {
    if (!fs.existsSync(file)) {
      return null;
    }
    return JSON.parse(fs.readFileSync(file, "utf-8"));
  } catch (e) {
    console.error(`⚠️  ${label} 讀取失敗，將忽略:`, e.message);
    return null;
  }
}

function loadCredentials() {
  const location = resolveCredentialsLocation();
  const creds = readJsonFile(location.file, `微信憑證(${location.source})`);
  if (!creds || !creds.baseUrl || !creds.botToken) {
    throw new Error(`找不到有效的微信憑證：${location.file}`);
  }
  return {
    ...creds,
    _source: location.source,
    _path: location.file,
  };
}

function normalizeContextCache(raw, fallbackUpdatedAt = null) {
  if (!raw || typeof raw !== "object") {
    return {};
  }
  const normalized = {};
  const defaultUpdatedAt = fallbackUpdatedAt || new Date().toISOString();
  for (const [userId, value] of Object.entries(raw)) {
    if (!userId) continue;
    if (typeof value === "string") {
      const token = value.trim();
      if (token) {
        normalized[userId] = {
          context_token: token,
          updated_at: defaultUpdatedAt,
          invalidated_at: null,
          invalid_reason: null,
        };
      }
      continue;
    }
    if (value && typeof value === "object") {
      const token = String(value.context_token || "").trim();
      if (!token) continue;
      normalized[userId] = {
        context_token: token,
        updated_at: String(value.updated_at || defaultUpdatedAt),
        invalidated_at: value.invalidated_at ? String(value.invalidated_at) : null,
        invalid_reason: value.invalid_reason ? String(value.invalid_reason) : null,
      };
    }
  }
  return normalized;
}

function loadContextCache() {
  const location = resolveContextCacheLocation();
  const raw = readJsonFile(location.file, `context cache(${location.source})`);
  if (!raw) {
    return {};
  }
  let fallbackUpdatedAt = null;
  try {
    fallbackUpdatedAt = fs.statSync(location.file).mtime.toISOString();
  } catch (_) {}
  return normalizeContextCache(raw, fallbackUpdatedAt);
}

function saveContextCache(cache) {
  const location = resolveContextCacheLocation();
  ensureParentDir(location.file);
  fs.writeFileSync(location.file, JSON.stringify(cache, null, 2), "utf-8");
}

function trimContextCache(cache) {
  const entries = Object.entries(cache).sort((a, b) => {
    const ta = new Date(a[1]?.updated_at || 0).getTime();
    const tb = new Date(b[1]?.updated_at || 0).getTime();
    return tb - ta;
  });
  if (entries.length <= MAX_CONTEXT_USERS) {
    return cache;
  }
  const trimmed = Object.fromEntries(entries.slice(0, MAX_CONTEXT_USERS));
  return trimmed;
}

function updateContextCache(cache, userId, contextToken) {
  if (!userId || !contextToken) return cache;
  const normalized = String(contextToken).trim();
  if (!normalized) return cache;

  const prev = cache[userId]?.context_token;
  if (prev === normalized && !cache[userId]?.invalidated_at && !cache[userId]?.invalid_reason) return cache;

  const next = {
    ...cache,
    [userId]: {
      context_token: normalized,
      updated_at: new Date().toISOString(),
      invalidated_at: null,
      invalid_reason: null,
    },
  };
  const trimmed = trimContextCache(next);
  saveContextCache(trimmed);
  return trimmed;
}

function buildContextInfo(cache, targetUserId, explicitContextToken) {
  const explicit = String(explicitContextToken || "").trim();
  if (explicit) {
    return {
      token: explicit,
      source: "request",
      ready: true,
      stale: false,
      invalid: false,
      reason: "request_context_token",
      updated_at: null,
      age_seconds: null,
      invalidated_at: null,
      invalid_reason: null,
    };
  }
  const cachedEntry = cache[targetUserId];
  const cached = cachedEntry?.context_token;
  if (cached) {
    const updatedAt = cachedEntry?.updated_at || null;
    const updatedMs = updatedAt ? Date.parse(updatedAt) : Number.NaN;
    const ageMs = Number.isFinite(updatedMs) ? Date.now() - updatedMs : Number.NaN;
    const stale = !Number.isFinite(ageMs) || ageMs > CONTEXT_MAX_AGE_MS;
    const invalidatedAt = cachedEntry?.invalidated_at || null;
    const invalidReason = cachedEntry?.invalid_reason || null;
    const invalid = Boolean(invalidatedAt || invalidReason);
    const reason = invalid
      ? (invalidReason || "invalidated_context_token")
      : (stale ? "stale_context_token" : "cached_context_token");
    return {
      token: cached,
      source: "cache",
      ready: !stale && !invalid,
      stale,
      invalid,
      reason,
      updated_at: updatedAt,
      age_seconds: Number.isFinite(ageMs) ? Math.max(0, Math.round(ageMs / 1000)) : null,
      invalidated_at: invalidatedAt,
      invalid_reason: invalidReason,
    };
  }
  return {
    token: "system_push",
    source: "fallback_system_push",
    ready: false,
    stale: true,
    invalid: false,
    reason: "missing_context_token",
    updated_at: null,
    age_seconds: null,
    invalidated_at: null,
    invalid_reason: null,
  };
}

function markContextInvalid(cache, targetUserId, reason = "invalidated_context_token") {
  const existing = cache[targetUserId];
  if (!targetUserId || !existing?.context_token) return cache;
  const next = {
    ...cache,
    [targetUserId]: {
      ...existing,
      invalidated_at: new Date().toISOString(),
      invalid_reason: String(reason || "invalidated_context_token"),
    },
  };
  saveContextCache(next);
  return next;
}

// fire-and-forget：通知 api_server 發飛書告警；本身錯誤不可阻斷主流程
function notifyContextInvalid(reason, target, traceId) {
  const dedupeKey = `${String(reason || "unknown")}::${String(target || "")}`;
  const now = Date.now();
  const lastSentAt = recentContextInvalidAlerts.get(dedupeKey) || 0;
  if (now - lastSentAt < CONTEXT_INVALID_NOTIFY_COOLDOWN_MS) {
    console.log(`⏭️ context_invalid 告警節流 reason=${reason} target=${target || "-"}`);
    return;
  }
  recentContextInvalidAlerts.set(dedupeKey, now);
  const payload = JSON.stringify({
    reason: String(reason || "unknown"),
    target: String(target || ""),
    trace_id: traceId ? String(traceId) : null,
  });
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 3000);
  fetch("http://127.0.0.1:3003/alert/wechat-context-invalid", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload,
    signal: ctrl.signal,
  })
    .catch((e) => {
      console.log(`⚠️ 通知 api_server 告警失敗 (reason=${reason}): ${e.message}`);
      recentContextInvalidAlerts.delete(dedupeKey);
    })
    .finally(() => clearTimeout(t));
}

function buildWaitingContextMessage(contextInfo) {
  if (contextInfo.reason === "stale_context_token") {
    return `stale context_token (${contextInfo.age_seconds ?? "?"}s old)`;
  }
  if (contextInfo.invalid) {
    return `invalid context_token (${contextInfo.invalid_reason || "invalidated_context_token"})`;
  }
  return "missing valid context_token";
}

function buildHealthPromptNeeded(contextInfo) {
  return !contextInfo.ready && ["missing_context_token", "stale_context_token", "invalidated_context_token", "send_context_invalid", "keepalive_context_invalid", "repair_context_invalid"].includes(contextInfo.reason);
}

function dropCachedContext(cache, targetUserId) {
  if (!targetUserId || !cache[targetUserId]) return cache;
  const next = { ...cache };
  delete next[targetUserId];
  saveContextCache(next);
  return next;
}

function splitTextByLength(text, maxLength = SEND_MAX_LENGTH) {
  if (text.length <= maxLength) return [text];
  const chunks = [];
  for (let i = 0; i < text.length; i += maxLength) {
    chunks.push(text.slice(i, i + maxLength));
  }
  return chunks;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function computeSendDelay(index) {
  if (index <= 0) return 0;
  const jitter = SEND_CHUNK_JITTER_MS > 0 ? Math.floor(Math.random() * (SEND_CHUNK_JITTER_MS + 1)) : 0;
  return SEND_CHUNK_DELAY_MS + jitter;
}

function getLatestReplaySuccess(targetUserId = null) {
  try {
    if (!fs.existsSync(DELIVERY_LOG_FILE)) {
      return null;
    }
    const lines = fs.readFileSync(DELIVERY_LOG_FILE, "utf-8").trim().split("\n");
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      const line = lines[i]?.trim();
      if (!line) continue;
      try {
        const entry = JSON.parse(line);
        if (entry?.status !== "replayed_success") continue;
        if (targetUserId && entry?.target !== targetUserId) continue;
        return {
          time: entry.time || null,
          queue_id: entry.queue_id || null,
          original_trace_id: entry.original_trace_id || null,
          target: entry.target || null,
          text_len: entry.text_len ?? null,
          trigger_source: entry.trigger_source || null,
          trigger_text: entry.trigger_text || "",
          context_source: entry.context_source || null,
          chunk_count: entry.chunk_count ?? null,
          ack_mode: entry.ack_mode || null,
        };
      } catch (_) {}
    }
  } catch (_) {}
  return null;
}

function getHealthDecision({ targetUser, contextInfo, pendingFailedCount }) {
  if (!targetUser) {
    return {
      action_required: true,
      recommended_action: "請先設定推送目標用戶",
      summary_status: "missing_target_user",
      summary_text: "尚未設定目標用戶，請先設定推送目標。",
    };
  }

  if (contextInfo.ready) {
    if (pendingFailedCount > 0) {
      return {
        action_required: true,
        recommended_action: "現在可以直接執行補發佇列",
        summary_status: "repair_available",
        summary_text: `目前可直接推送，但仍有 ${pendingFailedCount} 筆待補發，建議立即補發。`,
      };
    }

    return {
      action_required: false,
      recommended_action: "目前不需要額外操作",
      summary_status: "ready",
      summary_text: "Bridge 正常，可直接推送。",
    };
  }

  if (pendingFailedCount > 0) {
    return {
      action_required: true,
      recommended_action: "先到微信回覆 ping，等 context 刷新後再補發",
      summary_status: "waiting_for_context_refresh",
      summary_text: `目前有 ${pendingFailedCount} 筆待補發，但缺少有效 context，請先在微信發 ping 再補發。`,
    };
  }

  return {
    action_required: true,
    recommended_action: "先到微信回覆 ping 刷新推播上下文",
    summary_status: "not_ready",
    summary_text: "目前缺少有效 context，請在微信發 ping 刷新推播上下文。",
  };
}

function buildHealthRunbook(summaryStatus) {
  const runbooks = {
    ready: [
      "第 1 層：目前 Bridge 正常，可直接推送。",
      "第 2 層：若只是例行檢查，維持觀察即可，不需要額外操作。",
      "第 3 層：若要做回歸驗證，可執行 wechat_health_check.sh 或 wechat_bridge_selftest.py。",
    ],
    repair_available: [
      "第 1 層：context 已可用，先補發待送佇列。",
      "第 2 層：補發後確認 pending_failed_count 是否降為 0。",
      "第 3 層：再檢查 queue_id 是否已對上 replayed_success。",
    ],
    waiting_for_context_refresh: [
      "第 1 層：先到微信對 bot 回覆 ping，刷新 context。",
      "第 2 層：等待數秒後重新檢查 /health。",
      "第 3 層：若仍有待補發，再執行補發並確認 queue→replay 已完成。",
    ],
    not_ready: [
      "第 1 層：目前沒有有效 context，先到微信對 bot 回覆 ping。",
      "第 2 層：重新檢查 /health 是否變成 ready。",
      "第 3 層：若仍未恢復，再檢查 bridge 服務與 context cache。",
    ],
    missing_target_user: [
      "第 1 層：先設定推送目標用戶。",
      "第 2 層：重新檢查 /health 是否已出現 target_user。",
      "第 3 層：再重新執行健康檢查或推送流程。",
    ],
    unknown: [
      "第 1 層：先重新檢查 /health。",
      "第 2 層：再看 summary_status、recommended_action 與 pending_failed_count。",
      "第 3 層：必要時執行 wechat_health_check.sh 做完整自測。",
    ],
  };
  return runbooks[summaryStatus] || runbooks.unknown;
}

function getLatestDirectSuccess(targetUserId = null) {
  try {
    if (!fs.existsSync(DELIVERY_LOG_FILE)) {
      return null;
    }
    const lines = fs.readFileSync(DELIVERY_LOG_FILE, "utf-8").trim().split("\n");
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      const line = lines[i]?.trim();
      if (!line) continue;
      try {
        const entry = JSON.parse(line);
        if (entry?.status !== "success") continue;
        if (entry?.source === "chat_reply") continue;
        if (targetUserId && entry?.target !== targetUserId) continue;
        return {
          time: entry.time || null,
          trace_id: entry.trace_id || null,
          target: entry.target || null,
          text_len: entry.text_len ?? null,
          context_source: entry.context_source || null,
          chunk_count: entry.chunk_count ?? null,
          ack_mode: entry.ack_mode || null,
        };
      } catch (_) {}
    }
  } catch (_) {}
  return null;
}

function getLastContextRefresh(contextCache, targetUserId = null) {
  if (!targetUserId) return null;
  const entry = contextCache?.[targetUserId];
  if (!entry?.updated_at) return null;
  return {
    target: targetUserId,
    updated_at: entry.updated_at || null,
    invalidated_at: entry.invalidated_at || null,
    invalid_reason: entry.invalid_reason || null,
  };
}


function appendDeliveryLog(entry) {
  const line = JSON.stringify({ time: new Date().toISOString(), ...entry });
  fs.appendFileSync(DELIVERY_LOG_FILE, line + "\n", "utf-8");
}

function appendInboundAudit(entry) {
  ensureParentDir(INBOUND_AUDIT_FILE);
  const line = JSON.stringify({ time: new Date().toISOString(), ...entry });
  fs.appendFileSync(INBOUND_AUDIT_FILE, line + "\n", "utf-8");
}

function getLatestInbound(targetUserId = null) {
  try {
    if (!fs.existsSync(INBOUND_AUDIT_FILE)) {
      return null;
    }
    const lines = fs.readFileSync(INBOUND_AUDIT_FILE, "utf-8").trim().split("\n");
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      const line = lines[i]?.trim();
      if (!line) continue;
      try {
        const entry = JSON.parse(line);
        if (targetUserId && entry?.from_user_id !== targetUserId) continue;
        return entry;
      } catch (_) {}
    }
  } catch (_) {}
  return null;
}

function normalizeLogicalMeta(raw, fallbackLogicalId = null) {
  const meta = raw && typeof raw === "object" && !Array.isArray(raw) ? { ...raw } : {};
  const logicalId = String(meta.logical_id || fallbackLogicalId || buildQueueId("logical")).trim();
  meta.logical_id = logicalId;
  return meta;
}

function buildDeliveryManifest(meta, textLength, details = [], chunkCount = null) {
  return {
    logical_id: meta.logical_id || null,
    logical_text_len: textLength,
    news_sequence_index: meta.sequence_index ?? null,
    news_sequence_total: meta.sequence_total ?? null,
    range_start: meta.range_start ?? null,
    range_end: meta.range_end ?? null,
    category: meta.category ?? null,
    chunk_count: chunkCount ?? (Array.isArray(details) ? details.length : null),
    details,
    meta,
  };
}

function buildQueueId(prefix = "repair") {
  return `${prefix}-${Date.now()}-${crypto.randomBytes(4).toString("hex")}`;
}

function normalizeFailedQueue(raw) {
  if (!Array.isArray(raw)) {
    return [];
  }
  const normalized = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const target = String(item.target || "").trim();
    const text = String(item.text || "");
    if (!target || !text.trim()) continue;
    normalized.push({
      id: String(item.id || buildQueueId("legacy")),
      target,
      text,
      typing_seconds: Math.max(0, Number.parseInt(item.typing_seconds, 10) || 0),
      created_at: String(item.created_at || new Date().toISOString()),
      updated_at: String(item.updated_at || item.created_at || new Date().toISOString()),
      trace_id: String(item.trace_id || "").trim() || null,
      retry_count: Math.max(0, Number.parseInt(item.retry_count, 10) || 0),
      last_error: item.last_error ? String(item.last_error) : null,
      last_error_at: item.last_error_at ? String(item.last_error_at) : null,
      logical_id: String(item.logical_id || "").trim() || null,
      meta: item.meta && typeof item.meta === "object" && !Array.isArray(item.meta) ? item.meta : {},
    });
  }
  return normalized;
}

function trimFailedQueue(queue) {
  const now = Date.now();
  const filtered = queue.filter((entry) => {
    const createdAt = Date.parse(entry.created_at || "");
    return !Number.isFinite(createdAt) || now - createdAt <= FAILED_QUEUE_MAX_AGE_MS;
  });
  filtered.sort((a, b) => {
    const ta = Date.parse(a.created_at || "") || 0;
    const tb = Date.parse(b.created_at || "") || 0;
    return ta - tb;
  });
  if (filtered.length <= FAILED_QUEUE_MAX_ITEMS) {
    return filtered;
  }
  return filtered.slice(filtered.length - FAILED_QUEUE_MAX_ITEMS);
}

function loadFailedQueue() {
  const raw = readJsonFile(FAILED_QUEUE_FILE, "failed queue");
  return trimFailedQueue(normalizeFailedQueue(raw));
}

function saveFailedQueue(queue) {
  ensureParentDir(FAILED_QUEUE_FILE);
  fs.writeFileSync(FAILED_QUEUE_FILE, JSON.stringify(trimFailedQueue(queue), null, 2), "utf-8");
}

function getPendingFailedCount(queue, targetUserId = null) {
  return queue.filter((entry) => !targetUserId || entry.target === targetUserId).length;
}

function enqueueFailedDelivery({ target, text, typingSeconds, traceId, contextSource, errorMessage, ilinkRet, errcode, chunkCount = null, failedChunkIndex = null, logicalId = null, meta = {} }) {
  const queue = loadFailedQueue();
  const normalizedMeta = normalizeLogicalMeta(meta, logicalId);
  const duplicate = queue.find((entry) => entry.target === target && entry.text === text);
  if (duplicate) {
    const manifest = buildDeliveryManifest(normalizedMeta, text.length, [], chunkCount);
    appendDeliveryLog({
      status: "queued_for_repair_duplicate",
      queue_id: duplicate.id,
      trace_id: traceId,
      target,
      text_len: text.length,
      context_source: contextSource,
      ilink_ret: ilinkRet,
      errcode,
      msg: errorMessage,
      chunk_count: chunkCount,
      failed_chunk_index: failedChunkIndex,
      pending_failed_count: getPendingFailedCount(queue, target),
      ...manifest,
    });
    return {
      queued: false,
      duplicate: true,
      queue_id: duplicate.id,
      pending_count: getPendingFailedCount(queue, target),
      logical_id: duplicate.logical_id || normalizedMeta.logical_id,
      meta: duplicate.meta || normalizedMeta,
    };
  }
  const now = new Date().toISOString();
  const entry = {
    id: buildQueueId(),
    target,
    text,
    typing_seconds: Math.max(0, Number.parseInt(typingSeconds, 10) || 0),
    created_at: now,
    updated_at: now,
    trace_id: traceId || null,
    retry_count: 0,
    last_error: errorMessage || null,
    last_error_at: now,
    logical_id: normalizedMeta.logical_id,
    meta: normalizedMeta,
  };
  const nextQueue = [...queue, entry];
  const trimmedQueue = trimFailedQueue(nextQueue);
  saveFailedQueue(nextQueue);
  const manifest = buildDeliveryManifest(normalizedMeta, text.length, [], chunkCount);
  appendDeliveryLog({
    status: "queued_for_repair",
    queue_id: entry.id,
    trace_id: traceId,
    target,
    text_len: text.length,
    context_source: contextSource,
    ilink_ret: ilinkRet,
    errcode,
    msg: errorMessage,
    chunk_count: chunkCount,
    failed_chunk_index: failedChunkIndex,
    pending_failed_count: getPendingFailedCount(trimmedQueue, target),
    ...manifest,
  });
  return {
    queued: true,
    duplicate: false,
    queue_id: entry.id,
    pending_count: getPendingFailedCount(trimmedQueue, target),
    logical_id: entry.logical_id,
    meta: entry.meta,
  };
}

function isContextInvalidError(error) {
  const errMsg = String(error?.errmsg || error?.message || "").toLowerCase();
  return (
    error?.ret === -2
    || Number(error?.errcode) === -14
    || errMsg.includes("session timeout")
  );
}

function shouldInvalidateContextOnSendError(error) {
  if (!isContextInvalidError(error)) return false;
  const failedChunkIndex = Number(error?.failed_chunk_index ?? 1);
  return !Number.isFinite(failedChunkIndex) || failedChunkIndex <= 1;
}

function isRepairTriggerText(text) {
  const normalized = String(text || "").trim().toLowerCase();
  if (!normalized) return false;
  return ["/repair", "repair", "replay", "修復", "修复", "補發", "补发"].includes(normalized);
}

function buildInboundFingerprint(msg) {
  const userId = String(msg.from_user_id || "");
  const text = String(msg.item_list?.[0]?.text_item?.text || "").trim();
  const clientMsgId = String(msg.client_msg_id || msg.local_id || "");
  if (clientMsgId) return `${userId}:${clientMsgId}`;
  return `${userId}:${text}`;
}

function isDuplicateInbound(fingerprint) {
  const now = Date.now();
  const lastSeen = inboundMessageCache.get(fingerprint);
  if (lastSeen && now - lastSeen < INBOUND_DEDUPE_WINDOW_MS) return true;
  inboundMessageCache.set(fingerprint, now);
  if (inboundMessageCache.size > 500) {
    const cutoff = now - INBOUND_DEDUPE_WINDOW_MS;
    for (const [k, v] of inboundMessageCache) {
      if (v < cutoff) inboundMessageCache.delete(k);
    }
  }
  return false;
}

async function replayQueuedDeliveries({ targetUserId, explicitContextToken = "", triggerSource = "unknown", triggerText = "", _retryAfterInvalid = false }) {
  if (activeRepairTargets.has(targetUserId)) {
    console.log(`⏭️ repair 已在進行中，略過重複觸發 (${targetUserId}) source=${triggerSource}`);
    const queue = loadFailedQueue();
    const pendingCount = getPendingFailedCount(queue, targetUserId);
    const contextInfo = buildContextInfo(loadContextCache(), targetUserId, explicitContextToken);
    return {
      ready: contextInfo.ready,
      target: targetUserId,
      pending_before: pendingCount,
      pending_after: pendingCount,
      replayed_count: 0,
      attempted_count: 0,
      context_source: contextInfo.source,
      context_reason: contextInfo.reason,
      context_invalid: contextInfo.invalid,
      requires_user_refresh: buildHealthPromptNeeded(contextInfo),
      trigger_source: triggerSource,
      drain_completed: false,
      drain_stopped_reason: "repair_in_progress",
    };
  }
  activeRepairTargets.add(targetUserId);
  try {
    const currentCreds = loadCredentials();
    let contextCache = loadContextCache();
    const queue = loadFailedQueue();
    const pendingBefore = getPendingFailedCount(queue, targetUserId);
    const contextInfo = buildContextInfo(contextCache, targetUserId, explicitContextToken);

    if (!pendingBefore) {
      return {
        ready: contextInfo.ready,
        target: targetUserId,
        pending_before: 0,
        pending_after: 0,
        replayed_count: 0,
        attempted_count: 0,
        context_source: contextInfo.source,
        trigger_source: triggerSource,
        drain_completed: true,
        drain_stopped_reason: "empty_queue",
      };
    }

    if (!contextInfo.ready) {
      return {
        ready: false,
        target: targetUserId,
        pending_before: pendingBefore,
        pending_after: pendingBefore,
        replayed_count: 0,
        attempted_count: 0,
        context_source: contextInfo.source,
        context_reason: contextInfo.reason,
        context_invalid: contextInfo.invalid,
        requires_user_refresh: buildHealthPromptNeeded(contextInfo),
        trigger_source: triggerSource,
        drain_completed: false,
        drain_stopped_reason: "context_not_ready",
      };
    }

    const results = [];
    let replayedCount = 0;
    let attemptedCount = 0;
    const maxAttempts = Math.max(REPAIR_MAX_REPLAY, pendingBefore * 2, 10);
    let drainStoppedReason = "max_attempts";

    while (attemptedCount < maxAttempts) {
      const currentQueue = loadFailedQueue();
      const targetQueue = currentQueue.filter((entry) => entry.target === targetUserId);
      if (!targetQueue.length) {
        drainStoppedReason = "drained";
        break;
      }

      let progressThisRound = 0;
      let contextInvalidThisRound = false;
      const keepIds = new Set();
      let stopProcessingCurrentRound = false;

      for (const entry of currentQueue) {
        if (entry.target !== targetUserId || attemptedCount >= maxAttempts || stopProcessingCurrentRound) {
          keepIds.add(entry.id);
          continue;
        }

        attemptedCount += 1;
        try {
          const sendResult = await sendTextChunkedVerified({
            creds: currentCreds,
            toUserId: entry.target,
            text: entry.text,
            contextToken: contextInfo.token,
            logicalId: entry.logical_id,
            meta: entry.meta,
          });
          replayedCount += 1;
          progressThisRound += 1;
          results.push({ queue_id: entry.id, status: "success" });
          appendDeliveryLog({
            status: "replayed_success",
            queue_id: entry.id,
            original_trace_id: entry.trace_id,
            target: entry.target,
            text_len: entry.text.length,
            trigger_source: triggerSource,
            trigger_text: triggerText || "",
            context_source: contextInfo.source,
            chunk_count: sendResult.chunk_count,
            ack_mode: sendResult.ack_mode,
            pending_failed_count: Math.max(0, pendingBefore - replayedCount),
            ...buildDeliveryManifest(sendResult.meta, entry.text.length, sendResult.details, sendResult.chunk_count),
          });
        } catch (error) {
          const contextInvalid = shouldInvalidateContextOnSendError(error);
          const updatedEntry = {
            ...entry,
            retry_count: (entry.retry_count || 0) + 1,
            last_error: String(error?.message || error),
            last_error_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          };
          keepIds.add(updatedEntry.id);
          results.push({
            queue_id: entry.id,
            status: "error",
            context_invalid: contextInvalid,
            msg: String(error?.message || error),
          });
          appendDeliveryLog({
            status: "replayed_error",
            queue_id: entry.id,
            original_trace_id: entry.trace_id,
            target: entry.target,
            text_len: entry.text.length,
            trigger_source: triggerSource,
            trigger_text: triggerText || "",
            context_source: contextInfo.source,
            context_invalid: contextInvalid,
            failed_chunk_index: error?.failed_chunk_index ?? null,
            chunk_count: error?.chunk_count ?? null,
            ilink_ret: error?.ret ?? null,
            errcode: error?.errcode ?? null,
            msg: error?.message || String(error),
            pending_failed_count: Math.max(0, getPendingFailedCount(currentQueue, targetUserId) - progressThisRound),
            ...buildDeliveryManifest(updatedEntry.meta || error?.meta || entry.meta || {}, entry.text.length, error?.details || [], error?.chunk_count ?? null),
          });

          if (contextInvalid) {
            contextCache = markContextInvalid(contextCache, entry.target, "repair_context_invalid");
            if (_retryAfterInvalid || !explicitContextToken) {
              notifyContextInvalid("repair_context_invalid", entry.target, entry.original_trace_id || entry.trace_id || null);
            } else {
              console.log(`⏭️ repair_context_invalid 暫不告警，先等待 retry (${entry.target})`);
            }
            contextInvalidThisRound = true;
            stopProcessingCurrentRound = true;
            continue;
          }
        }
      }

      const nextQueue = currentQueue.filter((entry) => keepIds.has(entry.id));
      saveFailedQueue(nextQueue);

      if (contextInvalidThisRound) {
        if (!_retryAfterInvalid && explicitContextToken) {
          console.log(`⏳ context_invalid (ret:-2)，等待 2s 後重試 repair (${targetUserId})`);
          await new Promise((r) => setTimeout(r, 2000));
          contextCache = loadContextCache();
          const freshInfo = buildContextInfo(contextCache, targetUserId, explicitContextToken);
          if (freshInfo.ready) {
            activeRepairTargets.delete(targetUserId);
            const retryResult = await replayQueuedDeliveries({
              targetUserId,
              explicitContextToken,
              triggerSource: `${triggerSource}_retry`,
              triggerText,
              _retryAfterInvalid: true,
            });
            activeRepairTargets.add(targetUserId);
            return {
              ...retryResult,
              pending_before: pendingBefore,
              replayed_count: replayedCount + retryResult.replayed_count,
              attempted_count: attemptedCount + retryResult.attempted_count,
              context_retry: true,
            };
          }
        }
        drainStoppedReason = "context_invalid";
        break;
      }

      if (progressThisRound === 0) {
        drainStoppedReason = "no_progress";
        break;
      }
    }

    const finalQueue = loadFailedQueue();
    const pendingAfter = getPendingFailedCount(finalQueue, targetUserId);
    const drainCompleted = pendingAfter === 0;

    return {
      ready: true,
      target: targetUserId,
      pending_before: pendingBefore,
      pending_after: pendingAfter,
      replayed_count: replayedCount,
      attempted_count: attemptedCount,
      context_source: contextInfo.source,
      context_reason: contextInfo.reason,
      repair_blocked: drainStoppedReason === "context_invalid",
      requires_user_refresh: drainStoppedReason === "context_invalid",
      trigger_source: triggerSource,
      results,
      drain_completed: drainCompleted,
      drain_stopped_reason: drainCompleted ? "drained" : drainStoppedReason,
      context_retry: false,
    };
  } finally {
    activeRepairTargets.delete(targetUserId);
  }
}

function getCursorValue() {
  try {
    return fs.readFileSync(CURSOR_FILE, "utf-8");
  } catch (_) {
    return "";
  }
}

function buildSendHeaders(token, body) {
  const uin = Buffer.from(String(crypto.randomBytes(4).readUInt32BE(0)), "utf-8").toString("base64");
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
    AuthorizationType: "ilink_bot_token",
    "Content-Length": String(Buffer.byteLength(body, "utf-8")),
    "X-WECHAT-UIN": uin,
  };
}

async function postSendMessageRaw({ baseUrl, token, msg }) {
  const body = JSON.stringify({
    msg,
    base_info: { channel_version: CHANNEL_VERSION },
  });
  const url = new URL("ilink/bot/sendmessage", baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`).toString();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEND_TIMEOUT_MS);
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: buildSendHeaders(token, body),
      body,
      signal: controller.signal,
    });
    const text = await resp.text();
    if (!resp.ok) {
      throw new Error(`sendmessage HTTP ${resp.status}: ${text.slice(0, 200)}`);
    }
    let parsed = {};
    try {
      parsed = JSON.parse(text);
    } catch (_) {
      throw new Error(`sendmessage 非 JSON 回覆: ${text.slice(0, 200)}`);
    }
    return parsed;
  } finally {
    clearTimeout(timer);
  }
}

function buildClientId() {
  return `ilink-${Date.now()}-${crypto.randomBytes(6).toString("hex")}`;
}

// 自 ping：維持 context_token 有效
async function sendKeepAlive() {
  try {
    const creds = loadCredentials();
    const targetUser = creds.userId;
    if (!targetUser) {
      console.log("⚠️ 無法自 ping：未設定 userId");
      return;
    }

    // 檢查 token 年齡，若小於 KEEP_ALIVE_MIN_AGE_MS 則跳過
    const contextCache = loadContextCache();
    const cachedEntry = contextCache[targetUser];
    if (cachedEntry?.updated_at) {
      const ageMs = Date.now() - new Date(cachedEntry.updated_at).getTime();
      if (ageMs < KEEP_ALIVE_MIN_AGE_MS) {
        return; // Token 還很新，不需要刷新
      }
    }

    const contextInfo = buildContextInfo(contextCache, targetUser, null);
    if (!contextInfo.ready) {
      console.log("⏭️ 自 ping 跳過：無有效 context_token");
      return;
    }

    await sendTextChunkedVerified({
      creds,
      toUserId: targetUser,
      text: KEEP_ALIVE_TEXT,
      contextToken: contextInfo.token,
    });

    console.log(`✅ 自 ping 成功，token 已刷新 (${new Date().toLocaleTimeString()})`);
  } catch (e) {
    if (isContextInvalidError(e)) {
      const creds = loadCredentials();
      const targetUser = creds.userId;
      if (targetUser) {
        markContextInvalid(loadContextCache(), targetUser, "keepalive_context_invalid");
        notifyContextInvalid("keepalive_context_invalid", targetUser, null);
      }
    }
    console.log(`⚠️ 自 ping 失敗: ${e.message}`);
  }
}

// 啟動定時自 ping
function startKeepAliveTimer() {
  const hours = KEEP_ALIVE_INTERVAL_MS / 3600000;
  console.log(`⏰ 定時自 ping 已啟動（每 ${hours} 小時）`);
  // 延遲 1 分鐘後首次執行（避免啟動時搶資源）
  setTimeout(() => {
    sendKeepAlive();
    setInterval(sendKeepAlive, KEEP_ALIVE_INTERVAL_MS);
  }, 60000);
}

function createIlinkClient(creds) {
  return new ILinkClient({
    baseUrl: creds.baseUrl,
    token: creds.botToken,
  });
}

function buildUploadSafeFileName(filePath) {
  const parsed = path.parse(filePath);
  const ext = parsed.ext || ".bin";
  const base = parsed.name.normalize("NFKD").replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "");
  return `${base || "report"}${ext.toLowerCase()}`;
}

async function uploadFileToIlink({ creds, filePath }) {
  const stat = fs.statSync(filePath);
  const fileName = path.basename(filePath);
  const uploadFileName = buildUploadSafeFileName(filePath);
  const upload = await createIlinkClient(creds).getUploadUrl({
    file_name: uploadFileName,
    file_type: "file",
    file_size: stat.size,
  });
  if (!upload?.upload_url || !upload?.cdn_url) {
    throw new Error(`getUploadUrl failed: ${JSON.stringify(upload || {})}`);
  }

  const buffer = fs.readFileSync(filePath);
  const resp = await fetch(upload.upload_url, {
    method: "PUT",
    body: buffer,
    headers: {
      "Content-Length": String(buffer.length),
      "Content-Type": "application/pdf",
    },
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`upload file HTTP ${resp.status}: ${text.slice(0, 200)}`);
  }

  return {
    file_name: fileName,
    file_size: stat.size,
    cdn_url: upload.cdn_url,
  };
}

async function sendFileVerified({ creds, toUserId, filePath, contextToken }) {
  const uploadInfo = await uploadFileToIlink({ creds, filePath });
  const client = createIlinkClient(creds);
  await client.sendMedia(
    toUserId,
    {
      type: MessageItemType.FILE,
      file_item: {
        file_name: uploadInfo.file_name,
        file_size: uploadInfo.file_size,
        cdn_url: uploadInfo.cdn_url,
      },
    },
    contextToken,
  );
  return uploadInfo;
}

async function sendTextChunkedVerified({ creds, toUserId, text, contextToken, logicalId = null, meta = {} }) {
  const normalizedMeta = normalizeLogicalMeta(meta, logicalId);
  const chunks = splitTextByLength(text, SEND_MAX_LENGTH);
  const details = [];
  for (const [index, chunk] of chunks.entries()) {
    const delayMs = computeSendDelay(index);
    if (delayMs > 0) {
      await sleep(delayMs);
    }
    const clientId = buildClientId();
    const msg = {
      from_user_id: "",
      to_user_id: toUserId,
      client_id: clientId,
      message_type: 2,
      message_state: 2,
      context_token: contextToken,
      item_list: [
        {
          type: 1,
          text_item: { text: chunk },
        },
      ],
    };
    const payload = await postSendMessageRaw({
      baseUrl: creds.baseUrl,
      token: creds.botToken,
      msg,
    });
    const hasRet = Object.prototype.hasOwnProperty.call(payload || {}, "ret");
    const ret = hasRet ? Number(payload?.ret) : null;
    const detail = {
      client_id: clientId,
      ret: Number.isNaN(ret) ? null : ret,
      has_ret: hasRet,
      errmsg: payload?.errmsg || "",
      errcode: payload?.errcode ?? null,
      chunk_index: index + 1,
      chunk_total: chunks.length,
      chunk_length: chunk.length,
      range_start: index * SEND_MAX_LENGTH + 1,
      range_end: index * SEND_MAX_LENGTH + chunk.length,
      delay_ms: delayMs,
      logical_id: normalizedMeta.logical_id,
    };
    details.push(detail);
    if (hasRet && detail.ret !== 0) {
      const err = new Error(`ilink sendmessage ret=${detail.ret}${detail.errmsg ? ` (${detail.errmsg})` : ""}`);
      err.ret = detail.ret;
      err.errmsg = detail.errmsg;
      err.errcode = detail.errcode;
      err.details = details;
      err.failed_chunk_index = index + 1;
      err.chunk_count = chunks.length;
      err.logical_id = normalizedMeta.logical_id;
      err.meta = normalizedMeta;
      throw err;
    }
    if (!hasRet && (detail.errcode !== null || detail.errmsg)) {
      const err = new Error(`ilink sendmessage returned error fields without ret${detail.errmsg ? ` (${detail.errmsg})` : ""}`);
      err.ret = null;
      err.errmsg = detail.errmsg;
      err.errcode = detail.errcode;
      err.details = details;
      err.failed_chunk_index = index + 1;
      err.chunk_count = chunks.length;
      err.logical_id = normalizedMeta.logical_id;
      err.meta = normalizedMeta;
      throw err;
    }
  }
  const hasAnyRet = details.some((d) => d.has_ret);
  return {
    logical_id: normalizedMeta.logical_id,
    meta: normalizedMeta,
    chunk_count: chunks.length,
    details,
    ack_mode: hasAnyRet ? "ret_field" : "empty_body",
  };
}

async function start() {
  ensureLogDir();

  let bootCreds;
  try {
    bootCreds = loadCredentials();
    console.log(`✅ 已讀取微信憑證 source=${bootCreds._source}`);
  } catch (e) {
    console.error(`❌ ${e.message}`);
    process.exit(1);
  }

  let contextCache = loadContextCache();
  saveFailedQueue(loadFailedQueue());
  const cursorValue = getCursorValue();
  if (cursorValue) {
    console.log(`✅ Cursor 已恢復: ${cursorValue.slice(0, 20)}...`);
  } else {
    console.log("ℹ️  無舊 cursor");
  }

  let pollClient = null;
  if (ENABLE_POLLING) {
    pollClient = createIlinkClient(bootCreds);
    pollClient.cursor = cursorValue;
    console.log("🔄 已啟用微信輪詢");
  } else {
    console.log("ℹ️  已停用微信輪詢；bridge 目前以純推播 HTTP 模式執行");
  }

  const PYTHON_PORT = 3001;

  http.createServer((req, res) => {
    const requestUrl = new URL(req.url, `http://127.0.0.1:${PYTHON_PORT}`);
    if (req.method === "POST" && requestUrl.pathname === "/send") {
      let body = "";
      req.on("data", chunk => { body += chunk; });
      req.on("end", async () => {
        let traceId = `wx-${Date.now()}-${crypto.randomBytes(3).toString("hex")}`;
        let target = null;
        let contextInfo = { source: "unknown" };
        let messageText = "";
        let typingSecs = 0;
        let messageMeta = normalizeLogicalMeta({}, null);
        try {
          const currentCreds = loadCredentials();
          contextCache = loadContextCache();
          const { to_user, text, typing_seconds, context_token, logical_id, meta } = JSON.parse(body);
          target = to_user || currentCreds.userId || "filehelper";
          typingSecs = parseInt(typing_seconds) || 0;
          if (!text || typeof text !== "string" || !text.trim()) {
            throw new Error("text 不可為空");
          }
          messageText = text;
          messageMeta = normalizeLogicalMeta(meta, logical_id);
          contextInfo = buildContextInfo(contextCache, target, context_token);

          console.log(`[Bridge → 微信] trace=${traceId} logical_id=${messageMeta.logical_id} 推播給 ${target}，長度 ${text.length} 字，context=${contextInfo.source}`);

          if (!contextInfo.ready) {
            const waitingMsg = buildWaitingContextMessage(contextInfo);
            const queueResult = enqueueFailedDelivery({
              target,
              text: messageText,
              typingSeconds: typingSecs,
              traceId,
              contextSource: contextInfo.source,
              errorMessage: waitingMsg,
              ilinkRet: -2,
              errcode: null,
              logicalId: messageMeta.logical_id,
              meta: messageMeta,
            });
            const hint = "微信對話上下文已失效。請先在微信向機器人發一則訊息（例如：ping），刷新 context_token 後再推播。";
            console.warn(`⚠️ 微信推播延後 trace=${traceId}: ${waitingMsg}`);
            appendDeliveryLog({
              trace_id: traceId,
              status: "queued_waiting_context",
              target,
              text_len: text.length,
              context_source: contextInfo.source,
              context_reason: contextInfo.reason,
              context_invalid: contextInfo.invalid,
              context_updated_at: contextInfo.updated_at || null,
              context_age_seconds: contextInfo.age_seconds ?? null,
              context_invalidated_at: contextInfo.invalidated_at || null,
              msg: waitingMsg,
              ...buildDeliveryManifest(messageMeta, text.length),
            });
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end(JSON.stringify({
              status: "queued",
              trace_id: traceId,
              logical_id: queueResult.logical_id,
              meta: queueResult.meta,
              msg: waitingMsg,
              context_source: contextInfo.source,
              context_reason: contextInfo.reason,
              context_invalid: true,
              requires_user_refresh: true,
              queued_for_repair: true,
              repair_queue_id: queueResult.queue_id,
              pending_repair_count: queueResult.pending_count,
              delivery_state: "queued_waiting_context",
              hint,
              ...buildDeliveryManifest(queueResult.meta || messageMeta, text.length),
            }));
            return;
          }

          if (typingSecs > 0) {
            try {
              const typingClient = createIlinkClient(currentCreds);
              await typingClient.sendTyping(target, contextInfo.token);
            } catch (_) {}
            await new Promise(r => setTimeout(r, Math.min(typingSecs * 1000, 5000)));
          }

          const sendResult = await sendTextChunkedVerified({
            creds: currentCreds,
            toUserId: target,
            text,
            contextToken: contextInfo.token,
            logicalId: messageMeta.logical_id,
            meta: messageMeta,
          });
          appendDeliveryLog({
            trace_id: traceId,
            status: "success",
            target,
            text_len: text.length,
            context_source: contextInfo.source,
            context_known_users: Object.keys(contextCache).length,
            chunk_count: sendResult.chunk_count,
            ack_mode: sendResult.ack_mode,
            send_max_length: SEND_MAX_LENGTH,
            send_chunk_delay_ms: SEND_CHUNK_DELAY_MS,
            send_chunk_jitter_ms: SEND_CHUNK_JITTER_MS,
            ...buildDeliveryManifest(sendResult.meta, text.length, sendResult.details, sendResult.chunk_count),
          });

          const pendingBeforeDirectReplay = getPendingFailedCount(loadFailedQueue(), target);
          let postSendReplay = null;
          if (pendingBeforeDirectReplay > 0) {
            postSendReplay = await replayQueuedDeliveries({
              targetUserId: target,
              explicitContextToken: contextInfo.token,
              triggerSource: "post_send_success",
              triggerText: "",
            });
          }

          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({
            status: "success",
            trace_id: traceId,
            logical_id: sendResult.logical_id,
            meta: sendResult.meta,
            target,
            context_source: contextInfo.source,
            chunk_count: sendResult.chunk_count,
            ilink_ret: sendResult.ack_mode === "ret_field" ? 0 : null,
            ack_mode: sendResult.ack_mode,
            delivery_state: "sent",
            pending_before_replay: pendingBeforeDirectReplay,
            pending_after_replay: postSendReplay ? postSendReplay.pending_after : pendingBeforeDirectReplay,
            replayed_after_send: postSendReplay ? postSendReplay.replayed_count : 0,
            replay_drain_completed: postSendReplay ? Boolean(postSendReplay.drain_completed) : pendingBeforeDirectReplay === 0,
            replay_drain_stopped_reason: postSendReplay ? postSendReplay.drain_stopped_reason : (pendingBeforeDirectReplay === 0 ? "empty_queue" : "not_triggered"),
            ...buildDeliveryManifest(sendResult.meta, text.length, sendResult.details, sendResult.chunk_count),
          }));
        } catch (e) {
          console.error(`❌ 微信發送失敗 trace=${traceId}:`, e.message);
          const contextInvalid = shouldInvalidateContextOnSendError(e);
          let queueResult = { queued: false, duplicate: false, queue_id: null, pending_count: 0, logical_id: messageMeta.logical_id, meta: messageMeta };
          if (contextInvalid && target) {
            contextCache = markContextInvalid(contextCache, target, "send_context_invalid");
            notifyContextInvalid("send_context_invalid", target, traceId);
          }
          if (contextInvalid && target && messageText.trim()) {
            queueResult = enqueueFailedDelivery({
              target,
              text: messageText,
              typingSeconds: typingSecs,
              traceId,
              contextSource: contextInfo.source,
              errorMessage: e.message,
              ilinkRet: e.ret ?? null,
              errcode: e.errcode ?? null,
              chunkCount: e.chunk_count ?? null,
              failedChunkIndex: e.failed_chunk_index ?? null,
              logicalId: e.logical_id || messageMeta.logical_id,
              meta: e.meta || messageMeta,
            });
          }
          const hint = contextInvalid
            ? "微信對話上下文已失效。請先在微信向機器人發一則訊息（例如：ping），刷新 context_token 後再推播。"
            : "";
          appendDeliveryLog({
            trace_id: traceId,
            status: "error",
            target,
            context_source: contextInfo.source,
            context_reason: contextInfo.reason,
            context_invalid: contextInvalid,
            requires_user_refresh: contextInvalid,
            context_updated_at: contextInfo.updated_at || null,
            context_age_seconds: contextInfo.age_seconds ?? null,
            context_invalidated_at: contextInfo.invalidated_at || null,
            ilink_ret: e.ret ?? null,
            errcode: e.errcode ?? null,
            errmsg: e.errmsg || "",
            msg: e.message,
            failed_chunk_index: e.failed_chunk_index ?? null,
            chunk_count: e.chunk_count ?? null,
            pending_failed_count: queueResult.pending_count,
            repair_queue_id: queueResult.queue_id,
            ...buildDeliveryManifest(queueResult.meta || e.meta || messageMeta, messageText.length, e.details || [], e.chunk_count ?? null),
          });
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({
            status: "error",
            trace_id: traceId,
            logical_id: queueResult.logical_id || e.logical_id || messageMeta.logical_id,
            meta: queueResult.meta || e.meta || messageMeta,
            msg: e.message,
            ilink_ret: e.ret ?? null,
            errcode: e.errcode ?? null,
            context_source: contextInfo.source,
            context_reason: contextInfo.reason,
            context_invalid: contextInvalid,
            requires_user_refresh: contextInvalid,
            queued_for_repair: queueResult.queued || queueResult.duplicate,
            repair_queue_id: queueResult.queue_id,
            pending_repair_count: queueResult.pending_count,
            repair_queue_duplicate: queueResult.duplicate,
            delivery_state: queueResult.queued
              ? "queued_for_repair"
              : (queueResult.duplicate ? "queued_for_repair_duplicate" : "failed"),
            hint,
            ...buildDeliveryManifest(queueResult.meta || e.meta || messageMeta, messageText.length, e.details || [], e.chunk_count ?? null),
          }));
        }
      });
    } else if (req.method === "POST" && requestUrl.pathname === "/send-file") {
      let body = "";
      req.on("data", chunk => { body += chunk; });
      req.on("end", async () => {
        let traceId = `wx-file-${Date.now()}-${crypto.randomBytes(3).toString("hex")}`;
        let target = null;
        let contextInfo = { source: "unknown" };
        let filePath = "";
        try {
          const currentCreds = loadCredentials();
          contextCache = loadContextCache();
          const { to_user, file_path, context_token } = JSON.parse(body);
          target = to_user || currentCreds.userId || "filehelper";
          filePath = String(file_path || "").trim();
          if (!filePath) {
            throw new Error("file_path 不可為空");
          }
          if (!fs.existsSync(filePath)) {
            throw new Error(`file_path 不存在: ${filePath}`);
          }
          contextInfo = buildContextInfo(contextCache, target, context_token);
          if (!contextInfo.ready) {
            throw new Error(buildWaitingContextMessage(contextInfo));
          }

          console.log(`[Bridge → 微信] trace=${traceId} 發送檔案給 ${target}，file=${filePath}，context=${contextInfo.source}`);
          const sendResult = await sendFileVerified({
            creds: currentCreds,
            toUserId: target,
            filePath,
            contextToken: contextInfo.token,
          });
          appendDeliveryLog({
            trace_id: traceId,
            status: "success",
            target,
            delivery_kind: "file",
            file_name: sendResult.file_name,
            file_size: sendResult.file_size,
            context_source: contextInfo.source,
          });

          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({
            status: "success",
            trace_id: traceId,
            target,
            delivery_state: "sent",
            file_name: sendResult.file_name,
            file_size: sendResult.file_size,
            context_source: contextInfo.source,
          }));
        } catch (e) {
          console.error(`❌ 微信檔案發送失敗 trace=${traceId}:`, e.message);
          appendDeliveryLog({
            trace_id: traceId,
            status: "error",
            target,
            delivery_kind: "file",
            file_path: filePath,
            context_source: contextInfo.source,
            context_reason: contextInfo.reason,
            msg: e.message,
          });
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({
            status: "error",
            trace_id: traceId,
            msg: e.message,
            delivery_state: "failed",
            context_source: contextInfo.source,
            context_reason: contextInfo.reason,
          }));
        }
      });
    } else if (req.method === "POST" && requestUrl.pathname === "/repair") {
      let body = "";
      req.on("data", chunk => { body += chunk; });
      req.on("end", async () => {
        try {
          const payload = body ? JSON.parse(body) : {};
          const currentCreds = loadCredentials();
          const target = payload.to_user || currentCreds.userId || null;
          if (!target) {
            throw new Error("to_user 不可為空");
          }
          const repairResult = await replayQueuedDeliveries({
            targetUserId: target,
            explicitContextToken: String(payload.context_token || "").trim(),
            triggerSource: String(payload.trigger_source || "manual"),
            triggerText: String(payload.trigger_text || ""),
          });
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({
            status: "ok",
            ...repairResult,
            replay_ready: Boolean(repairResult.ready),
          }));
        } catch (e) {
          res.writeHead(500, { "Content-Type": "application/json" });
          res.end(JSON.stringify({
            status: "error",
            msg: e.message,
          }));
        }
      });
    } else if (req.method === "GET" && requestUrl.pathname === "/health") {
      try {
        const currentCreds = loadCredentials();
        contextCache = loadContextCache();
        const failedQueue = loadFailedQueue();
        const targetUser = requestUrl.searchParams.get("to_user") || currentCreds.userId || null;
        const contextInfo = buildContextInfo(contextCache, targetUser, null);
        const latestReplaySuccess = getLatestReplaySuccess(targetUser);
        const latestDirectSuccess = getLatestDirectSuccess(targetUser);
        const lastContextRefresh = getLastContextRefresh(contextCache, targetUser);
        const pendingFailedCount = getPendingFailedCount(failedQueue, targetUser);
        const healthDecision = getHealthDecision({
          targetUser,
          contextInfo,
          pendingFailedCount,
        });
        const latestInbound = getLatestInbound(targetUser);
        const healthRunbook = buildHealthRunbook(healthDecision.summary_status);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({
          status: "ok",
          cursor: getCursorValue().slice(0, 20) || null,
          target_user: targetUser,
          cached_context_users: Object.keys(contextCache).length,
          ready_for_push: Boolean(targetUser && contextInfo.ready),
          replay_ready: Boolean(targetUser && contextInfo.ready),
          requires_user_refresh: buildHealthPromptNeeded(contextInfo),
          context_source: contextInfo.source,
          context_reason: contextInfo.reason,
          context_updated_at: contextInfo.updated_at || null,
          context_age_seconds: contextInfo.age_seconds ?? null,
          context_max_age_seconds: Math.round(CONTEXT_MAX_AGE_MS / 1000),
          context_invalid: contextInfo.invalid,
          context_invalidated_at: contextInfo.invalidated_at || null,
          context_invalid_reason: contextInfo.invalid_reason || null,
          credentials_source: currentCreds._source,
          pending_failed_count: pendingFailedCount,
          polling_enabled: ENABLE_POLLING,
          latest_replayed_success: latestReplaySuccess,
          latest_direct_success: latestDirectSuccess,
          latest_inbound: latestInbound,
          last_context_refresh: lastContextRefresh,
          action_required: healthDecision.action_required,
          recommended_action: healthDecision.recommended_action,
          summary_status: healthDecision.summary_status,
          summary_text: healthDecision.summary_text,
          runbook_steps: healthRunbook,
        }));
      } catch (e) {
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({
          status: "error",
          msg: e.message,
        }));
      }
    } else {
      res.writeHead(404);
      res.end();
    }
  }).listen(PYTHON_PORT, () => {
    console.log(`🚀 HTTP Bridge 已啟動 http://127.0.0.1:${PYTHON_PORT}/send`);
  });

  if (!ENABLE_POLLING) {
    return;
  }

  console.log("🔄 開始監聽微信訊息...");

  // 啟動定時自 ping（維持 context_token 有效）
  startKeepAliveTimer();

  while (true) {
    try {
      const updates = await pollClient.poll();

      // poll 後立即儲存 cursor（即使沒有新訊息也要更新）
      if (updates.get_updates_buf && updates.get_updates_buf !== pollClient.cursor) {
        pollClient.cursor = updates.get_updates_buf;
        fs.writeFileSync(CURSOR_FILE, pollClient.cursor);
      }

      for (const msg of updates.msgs ?? []) {
        let refreshedContext = false;
        if (msg.from_user_id && msg.context_token) {
          const nextCache = updateContextCache(contextCache, msg.from_user_id, msg.context_token);
          if (nextCache !== contextCache) {
            contextCache = nextCache;
            refreshedContext = true;
            console.log(`🧩 已刷新 context_token 快取: ${msg.from_user_id}`);
          }
        }

        // 忽略無效訊息
        if (!msg.from_user_id || !msg.context_token) continue;

        const item = msg.item_list?.[0];
        if (!item || item.type !== MessageItemType.TEXT || !item.text_item?.text) continue;

        const text = item.text_item.text.trim();

        const fingerprint = buildInboundFingerprint(msg);
        if (isDuplicateInbound(fingerprint)) {
          console.log(`[去重] 已忽略重複訊息 ${msg.from_user_id}: ${text}`);
          continue;
        }

        console.log(`[收到] ${msg.from_user_id}: ${text}`);
        appendInboundAudit({
          from_user_id: msg.from_user_id,
          text,
          context_token_prefix: String(msg.context_token || "").slice(0, 24),
          refreshed_context: refreshedContext,
          cursor: pollClient.cursor ? String(pollClient.cursor).slice(0, 40) : null,
        });

        if (msg.from_user_id && msg.context_token) {
          try {
            const pendingBeforeReplay = getPendingFailedCount(loadFailedQueue(), msg.from_user_id);
            const shouldAutoReplay = pendingBeforeReplay > 0 && isRepairTriggerText(text);
            if (shouldAutoReplay) {
              const repairResult = await replayQueuedDeliveries({
                targetUserId: msg.from_user_id,
                explicitContextToken: msg.context_token,
                triggerSource: refreshedContext ? "polling_context_refresh" : "polling_auto",
                triggerText: text,
              });
              if (repairResult.replayed_count > 0) {
                console.log(`♻️ 已自動補發 ${repairResult.replayed_count} 則待送訊息給 ${msg.from_user_id}`);
              }
            }
          } catch (repairErr) {
            console.error(`⚠️ 自動補發失敗 ${msg.from_user_id}:`, repairErr.message);
          }
        }

        // 顯示「正在輸入」然後根據指令類型分流處理
        try {
          await pollClient.sendTyping(msg.from_user_id, msg.context_token);
        } catch (_) {}

        // 權限驗證
        if (!isUserAuthorized(msg.from_user_id)) {
          console.log(`[拒絕] 未授權用戶: ${msg.from_user_id}`);
          await pollClient.sendText(msg.from_user_id, "⛔ 您無權使用此服務", msg.context_token);
          continue;
        }

        // 比對指令前綴
        const matchedCmd = Object.keys(COMMAND_MAP)
          .sort((a, b) => b.length - a.length)
          .find(k => text.startsWith(k));

        if (matchedCmd) {
          await handleCommand(pollClient, msg, matchedCmd, text, COMMAND_MAP[matchedCmd]);
        } else {
          await handleChat(pollClient, msg, text);
        }
      }
    } catch (e) {
      console.error("⚠️ 輪詢異常，5 秒後重試:", e.message);
      await new Promise(r => setTimeout(r, 5000));
    }
  }
}

// ──────────────────────────────────────────────
// 指令處理：呼叫本地 Python API
// ──────────────────────────────────────────────
async function handleCommand(client, msg, cmdKey, fullText, cmdInfo) {
  const { endpoint, desc } = cmdInfo;

  // 從指令中抽股票代號（查籌碼 2330 → 2330）
  let stockId = "";
  if (cmdKey === "查籌碼") {
    stockId = fullText.replace(/^查籌碼/, "").trim();
    if (!stockId) {
      await client.sendText(msg.from_user_id, "📋 請附上股票代號，例如「查籌碼 2330」", msg.context_token);
      return;
    }
  }

  const apiBody = JSON.stringify({
    command: endpoint,
    ...(stockId ? { stock_id: stockId } : {}),
    to_user: msg.from_user_id,
    from_user: msg.from_user_id,
    context_token: msg.context_token,
    source: "wechat-bridge",
  });

  try {
    await client.sendText(msg.from_user_id, `🔍 正在查詢「${desc}」...`, msg.context_token);

    const resp = await fetch("http://127.0.0.1:3003", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: apiBody,
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const data = await resp.json();
    const reply = data.reply || data.error || "⚠️ 無法取得回覆";

    // 拆成多段發送，避免單條訊息過長
    await client.sendTextChunked(msg.from_user_id, reply, msg.context_token, 4000);
  } catch (err) {
    console.error(`[指令錯誤] ${cmdKey}:`, err.message);
    await client.sendText(
      msg.from_user_id,
      `❌ 執行「${desc}」失敗：${err.message}\n\n請確認以下服務已啟動：\n• Python API (api_server.py)\n• Orchestrator (orchestrator.py)`,
      msg.context_token
    );
  }
}

// ──────────────────────────────────────────────
// 通用聊天：轉發給 AI（透過 Python API）
// ──────────────────────────────────────────────
async function handleChat(client, msg, text) {
  const traceId = `wx-chat-${Date.now()}-${crypto.randomBytes(3).toString("hex")}`;
  const target = msg.from_user_id || null;
  const isDeepQuery = DEEP_QUERY_KEYWORDS.some(keyword => text.includes(keyword));
  const command = isDeepQuery ? "/deep-query" : "/chat";
  try {
    const payload = {
      text,
      command,
      source: "wechat-bridge",
      from_user: msg.from_user_id,
      context_token: msg.context_token,
      ...(isDeepQuery ? { query: text } : {}),
    };

    const resp = await fetch("http://127.0.0.1:3003", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const data = await resp.json();
    const reply = data.reply || "（暫時無法回覆）";
    const currentCreds = loadCredentials();
    const sendResult = await sendTextChunkedVerified({
      creds: currentCreds,
      toUserId: msg.from_user_id,
      text: reply,
      contextToken: msg.context_token,
    });
    appendDeliveryLog({
      trace_id: traceId,
      status: "success",
      source: "chat_reply",
      target,
      command,
      request_text_len: text.length,
      text_len: reply.length,
      context_source: "request",
      chunk_count: sendResult.chunk_count,
      ack_mode: sendResult.ack_mode,
      details: sendResult.details,
    });
  } catch (err) {
    console.error(`[聊天錯誤] trace=${traceId}:`, err.message);
    appendDeliveryLog({
      trace_id: traceId,
      status: "error",
      source: "chat_reply",
      target,
      command,
      request_text_len: text.length,
      context_source: "request",
      context_invalid: isContextInvalidError(err),
      ilink_ret: err.ret ?? null,
      errcode: err.errcode ?? null,
      errmsg: err.errmsg || "",
      msg: err.message,
      details: err.details || [],
    });
    try {
      await client.sendText(
        msg.from_user_id,
        "❌ AI 暫時無法回覆，請稍後再試",
        msg.context_token
      );
    } catch (sendErr) {
      console.error(`[聊天錯誤回覆失敗] trace=${traceId}:`, sendErr.message);
    }
  }
}

start().catch(e => {
  console.error("❌ Bridge 啟動失敗:", e);
  process.exit(1);
});
