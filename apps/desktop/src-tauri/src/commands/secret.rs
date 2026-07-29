/**
 * Secure Secret Store
 *
 * Stores sensitive values (LLM API keys) in the OS keychain via the `keyring`
 * crate (macOS Keychain / Windows Credential Manager / libsecret on Linux).
 *
 * Rationale: API keys must never be written to the renderer's localStorage
 * (where the zustand `persist` store lives). The Settings UI writes keys here,
 * and the LLM transport loads them into memory only at request time.
 */

const SERVICE: &str = "agentic-memory-ide";

fn entry(key: &str) -> Result<keyring::Entry, String> {
    keyring::Entry::new(SERVICE, key).map_err(|e| format!("Keychain error: {}", e))
}

#[tauri::command]
pub async fn secret_set(key: String, value: String) -> Result<(), String> {
    entry(&key)?
        .set_password(&value)
        .map_err(|e| format!("Failed to store secret: {}", e))
}

#[tauri::command]
pub async fn secret_get(key: String) -> Result<Option<String>, String> {
    match entry(&key)?.get_password() {
        Ok(v) => Ok(Some(v)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(format!("Failed to read secret: {}", e)),
    }
}

#[tauri::command]
pub async fn secret_has(key: String) -> Result<bool, String> {
    match entry(&key)?.get_password() {
        Ok(_) => Ok(true),
        Err(keyring::Error::NoEntry) => Ok(false),
        Err(e) => Err(format!("Failed to read secret: {}", e)),
    }
}

#[tauri::command]
pub async fn secret_delete(key: String) -> Result<(), String> {
    match entry(&key)?.delete_credential() {
        Ok(()) => Ok(()),
        Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(format!("Failed to delete secret: {}", e)),
    }
}
