/**
 * Error Reporter — Ring Buffer
 *
 * Stores the last 1000 errors in a circular buffer accessible from
 * the UI for debugging and crash reporting. Errors are never sent anywhere;
 * the user must explicitly export.
 */

export interface LoggedError {
  timestamp: number;
  source: string;
  message: string;
  stack?: string;
  context?: Record<string, unknown>;
}

const MAX_ERRORS = 1000;
const buffer: LoggedError[] = [];
const listeners = new Set<() => void>();

function notify() {
  for (const fn of listeners) fn();
}

export function logError(
  source: string,
  error: unknown,
  context?: Record<string, unknown>,
): void {
  const entry: LoggedError = {
    timestamp: Date.now(),
    source,
    message: error instanceof Error ? error.message : String(error),
    stack: error instanceof Error ? error.stack : undefined,
    context,
  };

  buffer.push(entry);
  if (buffer.length > MAX_ERRORS) {
    buffer.splice(0, buffer.length - MAX_ERRORS);
  }

  notify();
}

export function getErrors(): LoggedError[] {
  return [...buffer];
}

export function getRecentErrors(count = 50): LoggedError[] {
  return buffer.slice(-count);
}

export function clearErrors(): void {
  buffer.length = 0;
  notify();
}

export function exportErrors(): string {
  return JSON.stringify(buffer, null, 2);
}

export function onErrorLog(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/**
 * Wrap an async function with automatic error logging.
 */
export function withErrorLogging<T extends (...args: any[]) => Promise<any>>(
  source: string,
  fn: T,
): T {
  return (async (...args: any[]) => {
    try {
      return await fn(...args);
    } catch (err) {
      logError(source, err, { args: JSON.stringify(args).slice(0, 500) });
      throw err;
    }
  }) as T;
}
