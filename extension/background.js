// Service worker: the only place that talks to the local bridge.
//
// Content scripts inherit the page's origin, so a fetch from one to
// 127.0.0.1 would hit CORS and Chrome's Private Network Access rules. A
// request from the extension itself, covered by host_permissions, does not.

const BASE = "http://127.0.0.1:8765";
const TIMEOUT_MS = 15000;

async function call(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(BASE + path, {
      ...options,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) }
    });
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = { ok: false, error: `bridge returned ${response.status} with no JSON` };
    }
    if (!response.ok) {
      return { ok: false, error: body.error || `bridge returned ${response.status}`, status: response.status };
    }
    return body;
  } catch (error) {
    const offline = error.name === "AbortError"
      ? "bridge did not answer in time"
      : "bridge is not running";
    return {
      ok: false,
      offline: true,
      error: `${offline} — check: systemctl --user status spottytube-bridge`
    };
  } finally {
    clearTimeout(timer);
  }
}

const handlers = {
  health: () => call("/health"),
  card: () => call("/card"),
  sample: (msg) => call("/sample", { method: "POST", body: JSON.stringify(msg.payload) }),
  job: (msg) => call(`/jobs/${encodeURIComponent(msg.job_id)}`)
};

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const handler = handlers[msg && msg.type];
  if (!handler) {
    sendResponse({ ok: false, error: `unknown message ${msg && msg.type}` });
    return false;
  }
  handler(msg)
    .then(sendResponse)
    .catch((error) => sendResponse({ ok: false, error: String(error) }));
  return true; // async response
});
