// Read-only Hippo editor API inspector.
// Run on a disposable/test editor page. It never sends requests or saves changes itself.
// Start with: __hvApiInspector.mark("action label")
// After one manual action, mark the next action before continuing.
// Finish with: __hvApiInspector.download()
(() => {
  const match = location.pathname.match(/^\/deal-room\/edit\/(\d+)\/?$/);
  if (location.hostname !== "www.hippovideo.io" || !match) {
    console.error("Open https://www.hippovideo.io/deal-room/edit/<id> first.");
    return;
  }
  if (window.__hvApiInspector) {
    console.warn("Inspector is already running. Use __hvApiInspector.download() or .stop().");
    return;
  }

  const nativeFetch = window.fetch;
  const xhrMeta = new WeakMap();
  const observations = [];
  const mergeFieldPattern = /\$\{[^}]+\}/g;
  let currentAction = "initial editor read";
  let active = true;

  const mergeFields = value => [...new Set(String(value).match(mergeFieldPattern) || [])];

  function shape(value, depth = 0, seen = new WeakSet()) {
    if (value === null) return { type: "null" };
    if (typeof value === "string") return { type: "string", mergeFields: mergeFields(value) };
    if (typeof value === "number") return { type: "number" };
    if (typeof value === "boolean") return { type: "boolean" };
    if (typeof value !== "object") return { type: typeof value };
    if (seen.has(value)) return { type: "circular" };
    if (depth >= 7) return { type: Array.isArray(value) ? "array" : "object", truncated: true };
    seen.add(value);

    if (Array.isArray(value)) {
      const samples = value.slice(0, 8).map(item => shape(item, depth + 1, seen));
      const itemShapes = [...new Map(samples.map(item => [JSON.stringify(item), item])).values()];
      return { type: "array", length: value.length, items: itemShapes };
    }

    const fields = {};
    for (const [key, item] of Object.entries(value).slice(0, 250)) {
      fields[key] = shape(item, depth + 1, seen);
    }
    return { type: "object", fields, truncated: Object.keys(value).length > 250 || undefined };
  }

  function safePath(rawUrl) {
    try {
      const url = new URL(rawUrl, location.href);
      if (url.origin !== location.origin || !url.pathname.startsWith("/api/")) return null;
      const pathname = url.pathname
        .replace(/(\/deal-room\/edit\/)\d+/g, "$1<page-id>")
        .replace(/(\/templates\/)\d+/g, "$1<page-id>");
      return {
        path: pathname,
        queryKeys: [...new Set([...url.searchParams.keys()])].sort()
      };
    } catch {
      return null;
    }
  }

  function bodyShape(body) {
    if (body == null) return null;
    if (typeof body === "string") {
      try { return shape(JSON.parse(body)); } catch {}
      const params = new URLSearchParams(body);
      if ([...params.keys()].length) {
        const fields = {};
        for (const key of new Set([...params.keys()])) {
          const values = params.getAll(key);
          fields[key] = values.length > 1
            ? { type: "array", items: [...new Set(values.map(value => JSON.stringify(shape(value))))].map(v => JSON.parse(v)) }
            : shape(values[0]);
        }
        return { type: "form", fields };
      }
      return { type: "string", length: body.length };
    }
    if (body instanceof URLSearchParams) return bodyShape(body.toString());
    if (body instanceof FormData) {
      return {
        type: "form",
        fields: Object.fromEntries([...body.keys()].map(key => {
          const value = body.get(key);
          return [key, value instanceof File ? { type: "file" } : shape(value)];
        }))
      };
    }
    if (body instanceof Blob) return { type: "blob", mimeType: body.type || "unknown", size: body.size };
    return { type: typeof body };
  }

  function record(method, rawUrl, requestBody, response) {
    if (!active) return;
    const endpoint = safePath(rawUrl);
    if (!endpoint) return;
    const item = {
      action: currentAction,
      method: String(method || "GET").toUpperCase(),
      ...endpoint,
      status: response.status,
      request: bodyShape(requestBody),
      response: response.contentType.includes("json")
        ? response.bodyShape
        : { type: response.contentType || "unknown", bodyCaptured: false }
    };
    observations.push(item);
  }

  window.fetch = function(input, init = {}) {
    const rawUrl = input instanceof Request ? input.url : String(input);
    const method = init.method || (input instanceof Request ? input.method : "GET");
    const requestBody = init.body;
    return nativeFetch.apply(this, arguments).then(response => {
      const endpoint = safePath(rawUrl);
      if (!active || !endpoint) return response;
      const contentType = response.headers.get("content-type") || "";
      response.clone().text().then(text => {
        let parsed;
        try { parsed = contentType.includes("json") ? JSON.parse(text) : null; } catch {}
        record(method, rawUrl, requestBody, {
          status: response.status,
          contentType,
          bodyShape: parsed === undefined ? { type: "invalid-json" } : shape(parsed)
        });
      }).catch(() => {
        record(method, rawUrl, requestBody, {
          status: response.status,
          contentType,
          bodyShape: { type: "unreadable" }
        });
      });
      return response;
    });
  };

  const xhrOpen = XMLHttpRequest.prototype.open;
  const xhrSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    xhrMeta.set(this, { method, url: String(url) });
    return xhrOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    const xhr = this;
    const meta = xhrMeta.get(xhr) || {};
    const endpoint = safePath(meta.url || "");
    if (active && endpoint) {
      xhr.addEventListener("load", () => {
        const contentType = xhr.getResponseHeader("content-type") || "";
        let parsed;
        try { parsed = contentType.includes("json") ? JSON.parse(xhr.responseText) : null; } catch {}
        record(meta.method, meta.url, body, {
          status: xhr.status,
          contentType,
          bodyShape: parsed === undefined ? { type: contentType.includes("json") ? "invalid-json" : "not-json" } : shape(parsed)
        });
      }, { once: true });
    }
    return xhrSend.apply(this, arguments);
  };

  function download() {
    const report = {
      schemaVersion: 1,
      source: "Hippo deal-room editor",
      pagePath: "/deal-room/edit/<page-id>",
      initialTemplate: window.__hvApiInspector.initialTemplate,
      observedRequests: observations
    };
    const link = document.createElement("a");
    const objectUrl = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
    link.href = objectUrl;
    link.download = "hippo-editor-api-report.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    console.info(`Downloaded report with ${observations.length} sanitized API observations.`);
  }

  window.__hvApiInspector = {
    initialTemplate: null,
    mark(label) {
      if (typeof label !== "string" || !/^[a-z0-9 _-]{1,60}$/i.test(label)) {
        throw new Error("Use a short action label containing only letters, numbers, spaces, _ or -.");
      }
      currentAction = label;
      console.info(`Next API calls will be labeled: ${label}`);
    },
    download,
    stop() {
      active = false;
      window.fetch = nativeFetch;
      XMLHttpRequest.prototype.open = xhrOpen;
      XMLHttpRequest.prototype.send = xhrSend;
      delete window.__hvApiInspector;
      console.info("Inspector stopped. No page changes were made by the inspector.");
    }
  };

  (async () => {
    const response = await nativeFetch(
      `/api/video_page/templates/${match[1]}/editor?fromPreview=false`,
      { credentials: "same-origin", headers: { Accept: "application/json" } }
    );
    if (!response.ok) throw new Error(`Editor read failed: HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload.template || !Array.isArray(payload.template.sections)) {
      throw new Error("Editor response did not include template.sections.");
    }
    window.__hvApiInspector.initialTemplate = {
      responseShape: shape(payload),
      sectionCount: payload.template.sections.length,
      sections: payload.template.sections.map(section => ({
        name: typeof section.name,
        componentCount: Array.isArray(section.components) ? section.components.length : 0,
        componentTypes: [...new Set((section.components || []).map(component => component.type).filter(Boolean))],
        componentShapes: (section.components || []).map(component => ({
          type: component.type || "unknown",
          dataShape: shape(component.data)
        }))
      })),
      mergeFields: [...new Set(JSON.stringify(payload.template).match(mergeFieldPattern) || [])].sort()
    };
    console.info("Read-only structure captured. Use __hvApiInspector.mark('action') before each manual test action; download when done.");
  })().catch(error => {
    console.error("Inspector could not read the editor:", error.message);
  });
})();
