/**
 * Backend errors reach the UI as raw exception text. `Failed to reach Jira:
 * [Errno 8] nodename nor servname provided, or not known` is accurate and
 * unreadable — it wraps to three lines and tells a tester nothing they can
 * act on. This turns the failures that actually surface in the browser into a
 * short headline plus one line of what to do about it.
 *
 * The original string is handed back rather than discarded, so it can sit in
 * a tooltip instead of in the layout.
 *
 * Only the *shape* of a failure is rewritten, never its meaning: "could not
 * reach" stays "could not reach" and must never soften into "nothing found".
 * The repo's recurring bug is a transient failure recorded as a fact about the
 * data, and a friendlier error message is an easy place to reintroduce it.
 */

const UNREACHABLE = /^Failed to reach Jira:/i

// getaddrinfo failing, spelled the several ways the platforms spell it.
const DNS = /\[Errno 8\]|nodename nor servname|name or service not known|getaddrinfo|temporary failure in name resolution/i

const TIMEOUT = /timed out|timeout/i

/**
 * @param {string} raw - the `detail` string from the API
 * @returns {{title: string, detail: string|null, raw: string}}
 */
export function humanizeError(raw) {
  const text = typeof raw === 'string' ? raw.trim() : ''
  if (!text) return { title: 'Something went wrong', detail: null, raw: text }

  if (UNREACHABLE.test(text)) {
    if (DNS.test(text)) {
      return {
        title: "Can't reach Jira",
        detail: 'Check your Wi-Fi or VPN.',
        raw: text,
      }
    }
    if (TIMEOUT.test(text)) {
      return {
        title: 'Jira timed out',
        detail: 'Try again in a moment.',
        raw: text,
      }
    }
    return {
      title: "Can't reach Jira",
      detail: 'The connection failed.',
      raw: text,
    }
  }

  // Anything else is already a sentence written for a human (auth failures,
  // the "unexpected error" fallbacks) — show it as-is.
  return { title: text, detail: null, raw: text }
}
