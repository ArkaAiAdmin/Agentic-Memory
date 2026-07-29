/**
 * Auto-Updater Service
 *
 * Wraps Tauri's updater plugin for seamless updates.
 * Checks for updates on launch and notifies the user.
 * Uses dynamic imports to avoid type errors when plugins aren't installed.
 */

export interface UpdateInfo {
  version: string;
  releaseDate: string;
  releaseNotes?: string;
}

let updateAvailable: UpdateInfo | null = null;
let listeners = new Set<(info: UpdateInfo | null) => void>();

function notifyListener() {
  for (const fn of listeners) fn(updateAvailable);
}

/**
 * Check for updates from the configured endpoint.
 */
export async function checkForUpdates(): Promise<UpdateInfo | null> {
  try {
    // Dynamic import — only available when tauri-plugin-updater is configured
    const updater: any = await import(/* @vite-ignore */ "@tauri-apps/plugin-updater").catch(() => null);
    if (!updater?.check) return null;

    const update = await updater.check();
    if (update) {
      updateAvailable = {
        version: update.version,
        releaseDate: update.date ?? new Date().toISOString(),
        releaseNotes: update.body ?? undefined,
      };
      notifyListener();
      return updateAvailable;
    }
  } catch {
    // Updater not configured — silent
  }
  return null;
}

/**
 * Download and install the pending update.
 */
export async function installUpdate(): Promise<void> {
  try {
    const updater: any = await import(/* @vite-ignore */ "@tauri-apps/plugin-updater").catch(() => null);
    if (!updater?.check) return;

    const update = await updater.check();
    if (update) {
      await update.downloadAndInstall();
      // Restart the app
      const process: any = await import(/* @vite-ignore */ "@tauri-apps/plugin-process").catch(() => null);
      if (process?.relaunch) await process.relaunch();
    }
  } catch (err) {
    console.error("Update install failed:", err);
  }
}

/**
 * Subscribe to update availability.
 */
export function onUpdateAvailable(fn: (info: UpdateInfo | null) => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

/**
 * Get the current pending update if any.
 */
export function getPendingUpdate(): UpdateInfo | null {
  return updateAvailable;
}
