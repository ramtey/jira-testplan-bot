// Tiny session-storage helpers used across the app for soft persistence.
// Empty arrays and null/undefined values clear the key so JSON.parse on the
// next load doesn't need to special-case them.

export const loadStored = (key, fallback) => {
  try {
    const raw = sessionStorage.getItem(key)
    return raw === null ? fallback : JSON.parse(raw)
  } catch {
    return fallback
  }
}

export const saveStored = (key, value) => {
  try {
    if (value === null || value === undefined || (Array.isArray(value) && value.length === 0)) {
      sessionStorage.removeItem(key)
    } else {
      sessionStorage.setItem(key, JSON.stringify(value))
    }
  } catch {
    // storage quota / disabled — ignore
  }
}
