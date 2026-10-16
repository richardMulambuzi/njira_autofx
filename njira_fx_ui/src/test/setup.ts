/**
 * Vitest setup — node environment with a minimal DOM shim.
 *
 * The project has no jsdom/happy-dom installed, so we provide just enough of
 * a `window`/`localStorage` surface for the auth store (zustand persist) and
 * the api.ts token helpers to behave exactly as they do in the browser.
 */

class LocalStorageMock {
  private store = new Map<string, string>();

  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null;
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }

  removeItem(key: string): void {
    this.store.delete(key);
  }

  clear(): void {
    this.store.clear();
  }

  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }

  get length(): number {
    return this.store.size;
  }
}

const localStorageMock = new LocalStorageMock();

(globalThis as Record<string, unknown>).window = globalThis;
(globalThis as Record<string, unknown>).localStorage = localStorageMock;