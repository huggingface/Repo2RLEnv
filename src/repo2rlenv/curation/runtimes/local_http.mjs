/** Local control/tool transport with one explicit deadline and no fetch timeouts. */
import { request as httpRequest } from "node:http";

export const RUNTIME_REQUEST_TIMEOUT_MS = 60 * 60 * 1000;

export function describeError(error) {
  const messages = [];
  const seen = new Set();
  for (let current = error; current && !seen.has(current); current = current.cause) {
    seen.add(current);
    const message = current.message ?? (typeof current === "object" ? JSON.stringify(current) : String(current));
    messages.push(`${current.code ? `[${current.code}] ` : ""}${message}`);
  }
  return messages.join("; caused by: ") || "Unknown runtime error";
}

export async function requestJSON(url, { body, headers = {}, signal, timeoutMs = 60000, label = "Local HTTP" } = {}) {
  const target = new URL(url);
  if (target.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(target.hostname) || target.username || target.password) {
    throw new Error("Local bridge requests require a local HTTP URL");
  }
  const signals = [AbortSignal.timeout(timeoutMs), ...(signal ? [signal] : [])];
  const payload = body === undefined ? undefined : JSON.stringify(body);
  // Native fetch has an implicit 300s headers/body timeout even when the caller
  // supplies a longer AbortSignal. node:http uses our explicit deadline only.
  // Requests never follow redirects, so bridge credentials stay on loopback.
  return new Promise((resolve, reject) => {
    const request = httpRequest(target, {
      method: body === undefined ? "GET" : "POST",
      headers: { ...headers, "Content-Type": "application/json", ...(payload === undefined ? {} : { "Content-Length": Buffer.byteLength(payload) }) },
      signal: AbortSignal.any(signals),
      agent: false,
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.once("error", reject);
      response.once("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        if (response.statusCode < 200 || response.statusCode >= 300) {
          reject(new Error(`HTTP ${response.statusCode}: ${text.slice(-4000)}`));
          return;
        }
        try {
          resolve(text ? JSON.parse(text) : undefined);
        } catch (error) {
          reject(new Error(`Invalid JSON response: ${text.slice(-500)}`, { cause: error }));
        }
      });
    });
    request.once("error", reject);
    request.end(payload);
  }).catch((error) => {
    throw new Error(`${label} ${target.pathname}: ${describeError(error)}`, { cause: error });
  });
}
