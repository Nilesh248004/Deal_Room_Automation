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

// API_KEYS="ops:longrandomkey,sales-bot:otherkey"  (name:key pairs; a bare key gets the name "key1", "key2", ...)
const apiKeys = (env.API_KEYS || "").split(",").map(s => s.trim()).filter(Boolean).map((s, i) => {
  const at = s.indexOf(":");
  return at > 0 ? { name: s.slice(0, at), key: s.slice(at + 1) } : { name: "key" + (i + 1), key: s };
});

module.exports = {
  ROOT,
  host: env.HOST || "127.0.0.1",
  port: int(env.PORT, 8790),
  apiKeys,
  allowNoAuth: env.ALLOW_NO_AUTH === "1",
  dataDir: path.resolve(ROOT, env.DATA_DIR || "data"),
  python: env.PYTHON || "python3",
  maxConcurrent: int(env.MAX_CONCURRENT, 2),
  maxBodyBytes: int(env.MAX_BODY_MB, 25) * 1024 * 1024,
  webhookUrl: env.WEBHOOK_URL || "",
  stageTimeoutMs: {
    capture: int(env.CAPTURE_TIMEOUT_MS, 300000),
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
    "ALLOWED_HOTEL_DOMAINS", "ALLOW_HTTP", "CAPTURE_HAR", "AUTO_ROLLBACK"],
};
