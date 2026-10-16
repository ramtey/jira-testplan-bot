/**
 * The workflow banner's clause for what happened to the plan on Pull to
 * testing. The server posts it (src/app/services/auto_post.py); this only
 * words the outcome. A skip or failure is named, never left out: a tester
 * who reads "Moved to In Testing" alone assumes the plan went up.
 */
export const describeAutoPost = (autoPost) => {
  if (!autoPost) return ''
  const { action, reason, detail } = autoPost
  // The route posts after it responds; the Post control shows how it went.
  if (action === 'queued') return ' · posting the plan to Jira'
  if (action === 'posted') return ' · plan posted to Jira'
  if (action === 'failed') {
    return ` · plan NOT posted to Jira (${detail || reason || 'unknown error'})`
  }
  switch (reason) {
    case 'no_own_plan':
      return ' · no plan of its own yet, so nothing was posted'
    case 'empty_plan':
      return ' · plan has no cases, so it was not posted'
    case 'already_live':
    case 'plan_comment_exists':
      return ' · plan already on Jira'
    default:
      return reason ? ` · plan not posted (${reason})` : ''
  }
}
