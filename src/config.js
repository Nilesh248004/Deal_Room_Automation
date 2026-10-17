// Settings from the environment (and an optional .env file in the repo root). No dependencies.
"use strict";
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");

function loadDotEnv(file) {
  if (!fs.existsSync(file)) return;
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (!m || line.trim().startsWith("#")) continue;
    let v = m[2];
    if (/^".*"$|^'.*'$/.test(v)) v = v.slice(1, -1);
    if (process.env[m[1]] === undefined) process.env[m[1]] = v;
  }
}
loadDotEnv(path.join(ROOT, ".env"));

const env = process.env;
const int = (v, d) => (Number.isFinite(+v) && +v > 0 ? +v : d);

// API_KEYS="ops:longrandomkey,sales-bot:otherkey:viewer,lead:thirdkey:admin"  (name:key[:role] triples; a bare key gets the name "key1", "key2", ...)
// Roles: viewer = read only; operator (default, what every key could do before roles existed) = create, approve, edit, retry, cancel, roll back;
// admin = operator + approving brand-asset usage rights.
const ROLES = ["viewer", "operator", "admin"];
const apiKeys = (env.API_KEYS || "").split(",").map(s => s.trim()).filter(Boolean).map((s, i) => {
  const at = s.indexOf(":");
  if (!(at > 0)) return { name: "key" + (i + 1), key: s, role: "operator" };
  const rest = s.slice(at + 1), m = rest.match(/^(.+):(viewer|operator|admin)$/);
  return { name: s.slice(0, at), key: m ? m[1] : rest, role: m ? m[2] : "operator" };
});

module.exports = {
  ROOT, ROLES,
  host: env.HOST || "127.0.0.1",
  port: int(env.PORT, 8790),
  apiKeys,
  allowNoAuth: env.ALLOW_NO_AUTH === "1",
  dataDir: path.resolve(ROOT, env.DATA_DIR || "data"),
  python: env.PYTHON || "python3",
  maxConcurrent: int(env.MAX_CONCURRENT, 2),
  maxBodyBytes: int(env.MAX_BODY_MB, 25) * 1024 * 1024,
  webhookUrl: env.WEBHOOK_URL || "",
  // Transient failures (exit code 3) are retried with exponential backoff: base * 2^(attempt-1), capped, honouring Retry-After.
  retryMaxAttempts: int(env.RETRY_MAX_ATTEMPTS, 3),
  retryBaseMs: int(env.RETRY_BASE_MS, 2000),
  retryMaxMs: int(env.RETRY_MAX_MS, 60000),
  maxManualRetries: int(env.MAX_MANUAL_RETRIES, 5),
  // An identical request returns the existing job while it is active, or for this long after it succeeded (POST with "force": true to override).
  idempotencyWindowMs: int(env.IDEMPOTENCY_WINDOW_HOURS, 24) * 3600 * 1000,
  retentionDays: Number.isFinite(+env.RETENTION_DAYS) && +env.RETENTION_DAYS > 0 ? +env.RETENTION_DAYS : 0,
  stageTimeoutMs: {
    capture: int(env.CAPTURE_TIMEOUT_MS, 300000),
    assets: int(env.ASSETS_TIMEOUT_MS, 180000),
    load: int(env.HIPPO_TIMEOUT_MS, 120000),
    verify: int(env.HIPPO_TIMEOUT_MS, 120000),
    rollback: int(env.HIPPO_TIMEOUT_MS, 120000),
    default: int(env.STAGE_TIMEOUT_MS, 120000),
  },
  // Only these variables are passed to the Python stages (API keys for this server are never passed on).
  stageEnvKeys: ["PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "PLAYWRIGHT_BROWSERS_PATH", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HIPPO_BASE_URL", "HIPPO_API_TOKEN", "HIPPO_COOKIE", "HIPPO_CSRF",
    "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY",
    "ALLOWED_HOTEL_DOMAINS", "ALLOW_HTTP", "ALLOW_PRIVATE_HOSTS", "ROBOTS_POLICY", "ASSET_CHECK_POLICY", "ASSET_RIGHTS_POLICY", "ASSET_CACHE_HOURS", "CAPTURE_HAR", "AUTO_ROLLBACK"],
};
