/**
 * Pull image / video attachments out of a Jira ticket's linked PR bodies so
 * the walkthrough card can surface them for UAT. A developer who drops a
 * screenshot or screen-capture into their PR description almost always means
 * that as "here's how to see it" — the same intent the walkthrough section
 * exists to capture — but until now that content lived only on GitHub and
 * never reached the tester.
 *
 * We restrict extraction to GitHub-hosted upload URLs (user-attachments,
 * user-images.githubusercontent.com, etc.) so we don't accidentally treat a
 * logo hotlinked from a random CDN as walkthrough media.
 */

const IMG_EXT_RE = /\.(png|jpg|jpeg|gif|webp|svg|apng|avif)(?:\?|#|$)/i
const VIDEO_EXT_RE = /\.(mp4|webm|mov|m4v|ogg|ogv)(?:\?|#|$)/i

const GITHUB_UPLOAD_HOSTS = new Set([
  'user-images.githubusercontent.com',
  'private-user-images.githubusercontent.com',
  'user-videos.githubusercontent.com',
  'private-user-videos.githubusercontent.com',
])

function isGithubUpload(url) {
  try {
    const u = new URL(url)
    if (GITHUB_UPLOAD_HOSTS.has(u.hostname)) return true
    // The modern drag-and-drop attachment endpoint. GitHub sniffs content type
    // server-side, so these URLs are extension-less but always PR media.
    if (u.hostname === 'github.com' && u.pathname.startsWith('/user-attachments/assets/')) {
      return true
    }
    return false
  } catch {
    return false
  }
}

function classifyKind(url, hint) {
  if (hint) return hint
  if (VIDEO_EXT_RE.test(url)) return 'video'
  if (IMG_EXT_RE.test(url)) return 'image'
  // GitHub user-attachments assets carry no extension; we can't tell from the
  // URL alone. Surface them as generic attachments — the walkthrough card
  // renders those as a labelled link rather than trying (and failing) to embed.
  return 'attachment'
}

function filenameFromUrl(url, fallback) {
  try {
    const u = new URL(url)
    const last = u.pathname.split('/').filter(Boolean).pop()
    return last ? decodeURIComponent(last) : fallback
  } catch {
    return fallback
  }
}

function prLabelFromUrl(url) {
  if (!url) return null
  const match = /\/pull\/(\d+)/.exec(url)
  return match ? `#${match[1]}` : null
}

function pushIfNew(bucket, seen, entry) {
  if (!entry?.url || seen.has(entry.url)) return
  seen.add(entry.url)
  bucket.push(entry)
}

// Match ![alt](url "optional title") — the standard markdown image form.
const MD_IMAGE_RE = /!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g
// GitHub also lets you embed videos via raw <img>/<video> tags in issue bodies.
const HTML_IMG_RE = /<img\b[^>]*\bsrc=(?:"([^"]+)"|'([^']+)')/gi
const HTML_VIDEO_RE = /<video\b[^>]*\bsrc=(?:"([^"]+)"|'([^']+)')/gi
const HTML_SOURCE_RE = /<source\b[^>]*\bsrc=(?:"([^"]+)"|'([^']+)')/gi
// Bare URLs on their own line — how GitHub renders drag-and-drop attachments.
const BARE_URL_RE = /(https?:\/\/[^\s<>"')]+)/g

function extractFromText(text, source, bucket, seen) {
  if (!text) return

  let m
  MD_IMAGE_RE.lastIndex = 0
  while ((m = MD_IMAGE_RE.exec(text)) !== null) {
    const url = m[2]
    if (!isGithubUpload(url)) continue
    const alt = (m[1] || '').trim()
    pushIfNew(bucket, seen, {
      url,
      kind: classifyKind(url, 'image'),
      filename: alt || filenameFromUrl(url, 'image'),
      source,
    })
  }

  HTML_IMG_RE.lastIndex = 0
  while ((m = HTML_IMG_RE.exec(text)) !== null) {
    const url = m[1] || m[2]
    if (!isGithubUpload(url)) continue
    pushIfNew(bucket, seen, {
      url,
      kind: 'image',
      filename: filenameFromUrl(url, 'image'),
      source,
    })
  }

  for (const re of [HTML_VIDEO_RE, HTML_SOURCE_RE]) {
    re.lastIndex = 0
    while ((m = re.exec(text)) !== null) {
      const url = m[1] || m[2]
      if (!isGithubUpload(url)) continue
      pushIfNew(bucket, seen, {
        url,
        kind: 'video',
        filename: filenameFromUrl(url, 'video'),
        source,
      })
    }
  }

  BARE_URL_RE.lastIndex = 0
  while ((m = BARE_URL_RE.exec(text)) !== null) {
    const url = m[1]
    if (!isGithubUpload(url)) continue
    pushIfNew(bucket, seen, {
      url,
      kind: classifyKind(url),
      filename: filenameFromUrl(url, 'attachment'),
      source,
    })
  }
}

/**
 * Pull GitHub-hosted image / video URLs out of the linked PRs' descriptions
 * and conversation comments. Deduped by URL; source PR (title + url) travels
 * with each entry so the walkthrough card can attribute it.
 */
export function extractPrMedia(pullRequests) {
  const out = []
  const seen = new Set()
  const prs = Array.isArray(pullRequests) ? pullRequests : []
  for (const pr of prs) {
    if (!pr) continue
    const source = {
      pr_url: pr.url || null,
      pr_title: pr.title || null,
      pr_label: prLabelFromUrl(pr.url) || (pr.title ? 'PR' : null),
    }
    extractFromText(pr.github_description || '', source, out, seen)
    const comments = Array.isArray(pr.comments) ? pr.comments : []
    for (const c of comments) {
      extractFromText(c?.body || '', source, out, seen)
    }
  }
  return out
}
