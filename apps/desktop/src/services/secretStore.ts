/**
 * Secret Store (renderer side)
 *
 * Thin wrapper over the Rust keychain commands. API keys live in the OS
 * keychain, never in localStorage. Keys are addressed by provider type, e.g.
 * `apiKey:openai`.
 */

function keyOf(provider: string): string {
  return `apiKey:${provider}`;
}

async function core() {
  return import("@tauri-apps/api/core");
}

function isTauri(): boolean {
  return (
    typeof window !== "undefined" &&
    Boolean((window as any).__TAURI_INTERNALS__ || (window as any).__TAURI__)
  );
}

export async function setApiKey(provider: string, value: string): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await core();
  await invoke("secret_set", { key: keyOf(provider), value });
}

export async function getApiKey(provider: string): Promise<string | null> {
  if (!isTauri()) return null;
  const { invoke } = await core();
  return (await invoke<string | null>("secret_get", { key: keyOf(provider) })) ?? null;
}

export async function hasApiKey(provider: string): Promise<boolean> {
  if (!isTauri()) return false;
  const { invoke } = await core();
  return await invoke<boolean>("secret_has", { key: keyOf(provider) });
}

export async function deleteApiKey(provider: string): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await core();
  await invoke("secret_delete", { key: keyOf(provider) });
}
