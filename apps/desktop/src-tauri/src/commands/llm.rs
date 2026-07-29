/**
 * LLM HTTP Transport
 *
 * Proxies LLM API calls through the Rust backend instead of the webview.
 * Two problems this solves:
 *   1. CORS — cloud LLM endpoints (OpenAI/Anthropic/Google) reject browser-origin
 *      requests; a native reqwest client has no such restriction.
 *   2. Network isolation — the request never touches the webview's network stack.
 *
 * Streaming is preserved: response bytes are forwarded to the frontend over a
 * Tauri `Channel`, so the existing SSE parsers in `@ami/llm` keep working. Bytes
 * are sent raw (Vec<u8>) rather than as text so multi-byte UTF-8 sequences that
 * straddle a chunk boundary are reassembled by the frontend's TextDecoder.
 */

use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::LazyLock;
use std::time::Duration;
use tauri::ipc::Channel;

fn default_method() -> String {
    "POST".to_string()
}

#[derive(Deserialize)]
pub struct LlmFetchRequest {
    pub url: String,
    #[serde(default = "default_method")]
    pub method: String,
    #[serde(default)]
    pub headers: HashMap<String, String>,
    #[serde(default)]
    pub body: Option<String>,
}

#[derive(Clone, Serialize)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum LlmFetchEvent {
    Head {
        status: u16,
        headers: HashMap<String, String>,
    },
    Chunk {
        data: Vec<u8>,
    },
    End,
    Error {
        message: String,
    },
}

static HTTP_CLIENT: LazyLock<reqwest::Client> = LazyLock::new(|| {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(15))
        .timeout(Duration::from_secs(300))
        .build()
        .expect("Failed to build HTTP client")
});

const BODY_SIZE_LIMIT: u64 = 50 * 1024 * 1024;

fn validate_llm_url(url_str: &str) -> Result<(), String> {
    let url = reqwest::Url::parse(url_str).map_err(|e| format!("Invalid URL: {}", e))?;

    let host = url.host_str().ok_or_else(|| "URL has no host".to_string())?;

    let is_localhost = host == "localhost" || host == "127.0.0.1" || host == "::1";

    if is_localhost {
        let port = url.port().unwrap_or(80);
        let allowed_ports: [u16; 3] = [1234, 11434, 4000];
        if !allowed_ports.contains(&port) {
            return Err(format!(
                "Localhost port {} is not allowed. Allowed ports: {:?}",
                port, allowed_ports
            ));
        }
        if url.scheme() != "http" && url.scheme() != "https" {
            return Err("Only http or https scheme is allowed for localhost".to_string());
        }
    } else {
        let allowed_hosts: &[&str] = &[
            "api.openai.com",
            "api.anthropic.com",
            "generativelanguage.googleapis.com",
            "openrouter.ai",
        ];

        #[cfg(debug_assertions)]
        {
            // In debug mode, allow all remote hosts for development.
        }
        #[cfg(not(debug_assertions))]
        {
            if !allowed_hosts.contains(&host) {
                return Err(format!(
                    "Host '{}' is not in the allowed list. Allowed hosts: {:?}",
                    host, allowed_hosts
                ));
            }
        }

        if url.scheme() != "https" {
            return Err("Only https scheme is allowed for remote URLs".to_string());
        }
    }

    Ok(())
}

#[tauri::command]
pub async fn llm_fetch(
    request: LlmFetchRequest,
    on_event: Channel<LlmFetchEvent>,
) -> Result<(), String> {
    validate_llm_url(&request.url)?;

    if let Some(ref body) = request.body {
        if body.len() as u64 > BODY_SIZE_LIMIT {
            return Err(format!("Body size exceeds limit of {} bytes", BODY_SIZE_LIMIT));
        }
    }

    let method = reqwest::Method::from_bytes(request.method.to_uppercase().as_bytes())
        .map_err(|e| format!("Invalid HTTP method: {}", e))?;

    let mut builder = HTTP_CLIENT.request(method, &request.url);
    for (name, value) in &request.headers {
        builder = builder.header(name, value);
    }
    if let Some(body) = request.body {
        builder = builder.body(body);
    }

    let response = match builder.send().await {
        Ok(r) => r,
        Err(e) => {
            let msg = format!("Request failed: {}", e);
            let _ = on_event.send(LlmFetchEvent::Error { message: msg.clone() });
            return Err(msg);
        }
    };

    let status = response.status().as_u16();
    let mut headers = HashMap::new();
    for (name, value) in response.headers().iter() {
        if let Ok(v) = value.to_str() {
            headers.insert(name.as_str().to_string(), v.to_string());
        }
    }
    on_event
        .send(LlmFetchEvent::Head { status, headers })
        .map_err(|e| format!("Channel send failed: {}", e))?;

    let mut stream = response.bytes_stream();
    while let Some(item) = stream.next().await {
        match item {
            Ok(bytes) => {
                if on_event
                    .send(LlmFetchEvent::Chunk {
                        data: bytes.to_vec(),
                    })
                    .is_err()
                {
                    break;
                }
            }
            Err(e) => {
                let msg = format!("Stream read failed: {}", e);
                let _ = on_event.send(LlmFetchEvent::Error { message: msg.clone() });
                return Err(msg);
            }
        }
    }

    let _ = on_event.send(LlmFetchEvent::End);
    Ok(())
}
