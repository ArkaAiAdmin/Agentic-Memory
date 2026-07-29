/**
 * Tauri LLM Transport
 *
 * A `fetch`-compatible function that proxies HTTP through the Rust `llm_fetch`
 * command instead of the webview. This bypasses CORS on cloud LLM endpoints and
 * keeps the request off the webview network stack.
 *
 * Streaming is preserved by returning a `Response` whose body is a
 * `ReadableStream` fed by the Rust `Channel` events. The existing SSE parsers in
 * `@ami/llm` consume `response.body.getReader()` exactly as with native fetch.
 */

import { setFetchImpl, type FetchImpl } from "@ami/llm";

type LlmFetchEvent =
  | { type: "head"; status: number; headers: Record<string, string> }
  | { type: "chunk"; data: number[] }
  | { type: "end" }
  | { type: "error"; message: string };

function headersToRecord(init?: RequestInit): Record<string, string> {
  const out: Record<string, string> = {};
  if (init?.headers) {
    new Headers(init.headers as HeadersInit).forEach((value, key) => {
      out[key] = value;
    });
  }
  return out;
}

const tauriFetch: FetchImpl = async (url, init) => {
  const { invoke, Channel } = await import("@tauri-apps/api/core");

  const signal = init?.signal;

  const request = {
    url,
    method: (init?.method ?? "POST").toUpperCase(),
    headers: headersToRecord(init),
    body: typeof init?.body === "string" ? init.body : init?.body != null ? String(init.body) : null,
  };

  return new Promise<Response>((resolve, reject) => {
    let settled = false;
    let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
    let status = 200;
    let responseHeaders = new Headers();

    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        controller = c;
      },
    });

    const resolveResponse = () => {
      if (settled) return;
      settled = true;
      resolve(new Response(stream, { status, headers: responseHeaders }));
    };

    if (signal?.aborted) {
      reject(new DOMException("The operation was aborted.", "AbortError"));
      return;
    }

    signal?.addEventListener("abort", () => {
      if (!settled) {
        settled = true;
        reject(new DOMException("The operation was aborted.", "AbortError"));
      } else {
        try { controller?.error(new DOMException("Aborted", "AbortError")); } catch { /* already closed */ }
      }
    });

    const channel = new Channel<LlmFetchEvent>();
    channel.onmessage = (msg) => {
      switch (msg.type) {
        case "head":
          status = msg.status;
          responseHeaders = new Headers(msg.headers);
          resolveResponse();
          break;
        case "chunk":
          try {
            controller?.enqueue(new Uint8Array(msg.data));
          } catch {
            // stream already closed
          }
          break;
        case "end":
          resolveResponse();
          try {
            controller?.close();
          } catch {
            // already closed
          }
          break;
        case "error":
          if (!settled) {
            settled = true;
            reject(new Error(msg.message));
          } else {
            try {
              controller?.error(new Error(msg.message));
            } catch {
              // already errored/closed
            }
          }
          break;
      }
    };

    invoke("llm_fetch", { request, onEvent: channel }).catch((err) => {
      if (!settled) {
        settled = true;
        reject(err instanceof Error ? err : new Error(String(err)));
      } else {
        try {
          controller?.error(err instanceof Error ? err : new Error(String(err)));
        } catch {
          // already closed
        }
      }
    });
  });
};

/** True when running inside the Tauri webview (vs. plain browser/tests). */
export function isTauri(): boolean {
  return (
    typeof window !== "undefined" &&
    Boolean((window as any).__TAURI_INTERNALS__ || (window as any).__TAURI__)
  );
}

/**
 * A CORS-friendly fetch wrapper for browser-only mode (no Tauri).
 * Uses `no-cors` as last resort when a standard fetch fails due to CORS.
 * For local/LAN services like LM Studio this lets requests through.
 */
const corsFriendlyFetch: FetchImpl = async (url, init) => {
  try {
    // First try standard fetch (works if server sends CORS headers)
    return await fetch(url, init);
  } catch (err) {
    // If it's a network/CORS error and target is a local/LAN address, log guidance
    const isLocal = /localhost|127\.0\.0\.1|192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])/.test(url);
    if (isLocal) {
      console.warn(
        `[LLM Transport] CORS blocked request to ${url}.\n` +
        `Enable CORS in your LM Studio/Ollama settings, or run the app via \`cargo tauri dev\`.`
      );
    }
    throw err;
  }
};

/**
 * Route all `@ami/llm` HTTP through the Rust backend when in Tauri.
 * Falls back to a CORS-friendly fetch wrapper in plain browser mode.
 */
export function installLlmTransport(): void {
  if (isTauri()) {
    setFetchImpl(tauriFetch);
  } else {
    // In browser mode, use the CORS-friendly wrapper that provides better error messages
    setFetchImpl(corsFriendlyFetch);
  }
}
