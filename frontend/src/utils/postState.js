/**
 * How the single-ticket Post control reads, from what the server says is live.
 *
 * Auto-post (src/app/services/auto_post.py) handles the first post when the
 * ticket enters testing, so the button's job is everything it deliberately
 * leaves alone: a regenerated version behind a live one, a post that failed,
 * a plan posted before testing starts. It is loud only when there is
 * something to do.
 *
 * `postState` is the `post_state` of /runs/by-ticket, tagged with the ticket
 * it was fetched for. Null means the server could not say, which is not "not
 * live": the control falls back to the plain button it always was.
 */

const UNKNOWN = {
  mode: 'unknown',
  prominent: true,
  label: 'Post to Jira',
  buttonText: null,
  hint: 'Updates the existing bot comment instead of duplicating.',
}

// The chip and the sentence for an auto-post that did not put this plan up
// cleanly, or null when the outcome needs no explanation here.
const describeMiss = (last) => {
  switch (last.reason) {
    case 'not_recorded':
      return {
        warning: 'Not recorded',
        hint: `The plan is on the ticket, but the app could not record it as live (${last.detail || 'unknown error'}). Posting again updates that comment.`,
      }
    case 'stale_plan':
      return {
        warning: 'Not auto-posted',
        hint: `Not auto-posted: ${last.detail}. Regenerate against the merged code, or post this version as it is.`,
      }
    case 'freshness_unknown':
      return {
        warning: 'Not auto-posted',
        hint: `Not auto-posted: could not confirm the plan is newer than the merged code (${last.detail}).`,
      }
    case 'empty_plan':
      return { warning: 'Not auto-posted', hint: 'Not auto-posted: the plan has no cases.' }
    case 'plan_comment_exists':
      return {
        warning: 'Not auto-posted',
        hint: 'Not auto-posted: the ticket already has a plan comment the app did not record. Posting replaces it.',
      }
    default:
      if (last.action === 'failed') {
        return {
          warning: 'Not auto-posted',
          hint: `Not auto-posted: ${last.detail || last.reason || 'the post failed'}.`,
        }
      }
      return null
  }
}

// An auto-post that stopped partway still marks the plan live — its first
// comment landed — so "live" alone would hide an incomplete plan. Only the
// outcome of the post that is live now counts: a later re-post moves
// `posted_at` past it.
const INCOMPLETE = new Set(['partial', 'truncated'])
const incompleteLivePost = (last, live) => {
  if (!last || last.action !== 'failed' || !INCOMPLETE.has(last.reason)) return false
  if (last.plan_id !== live.plan_id) return false
  const at = Date.parse(last.at)
  const posted = Date.parse(live.posted_at)
  return !Number.isNaN(at) && !Number.isNaN(posted) && at >= posted
}

// Whether `planId` was written for `ticketKey` alone, from the run history.
// A ticket covered only by a batch plan shows that plan in the single-ticket
// view, and posting it there would put the batch plan in the ticket's own
// slot — so anything not provably the ticket's own gets the plain button.
export const isOwnPlan = (planId, ticketKey, runs) => {
  if (planId == null || !ticketKey) return false
  const run = (runs || []).find((r) => r?.plan_id === planId)
  const keys = Array.isArray(run?.ticket_keys) ? run.ticket_keys : []
  return keys.length === 1 && String(keys[0]).toUpperCase() === String(ticketKey).toUpperCase()
}

export const planPostState = ({ plan, ticketKey, ticketStatus, postState, runs }) => {
  const planId = plan?.plan_id ?? null
  if (!postState || planId == null) return UNKNOWN
  if (!isOwnPlan(planId, ticketKey, runs)) return UNKNOWN
  // Fetched for another ticket — a late response, or a cache. Never shown.
  if (!ticketKey || postState.ticketKey !== String(ticketKey).toUpperCase()) return UNKNOWN

  const live = postState.live_own_plan
  const last = postState.last_auto_post
  if (live && live.plan_id === planId && incompleteLivePost(last, live)) {
    return {
      mode: 'live_incomplete',
      prominent: true,
      label: 'Re-post to Jira',
      buttonText: 'Re-post to Jira',
      warning: 'Incomplete in Jira',
      hint: `The auto-post stopped partway: ${last.detail}. Re-post to put the whole plan up.`,
    }
  }
  if (live && live.plan_id === planId) {
    return {
      mode: 'live',
      prominent: false,
      label: `Re-post v${live.version} to Jira`,
      buttonText: null,
      chip: `Live in Jira · v${live.version}`,
      hint: 'This version is the ticket’s plan comment. Re-post only if the comment was edited or deleted in Jira.',
    }
  }
  if (live) {
    const from = live.version
    const to = plan?.version
    return {
      mode: 'outdated',
      prominent: true,
      label: to != null ? `Update Jira · v${from} → v${to}` : 'Update Jira',
      buttonText: to != null ? `Update Jira · v${from} → v${to}` : 'Update Jira',
      hint: `Jira still shows v${from}. A live plan is never replaced automatically, so this version goes up only when you post it.`,
    }
  }

  const miss = last && last.plan_id === planId ? describeMiss(last) : null
  if (miss) {
    return {
      mode: 'auto_missed',
      prominent: true,
      label: 'Post to Jira',
      buttonText: 'Post to Jira',
      warning: miss.warning,
      hint: miss.hint,
    }
  }

  const autoStatus = postState.auto_post_status
  const queueStatus = postState.queue_status
  if (
    autoStatus && queueStatus && ticketStatus &&
    String(ticketStatus).toLowerCase() === String(queueStatus).toLowerCase()
  ) {
    return {
      mode: 'auto_pending',
      prominent: false,
      label: 'Post now',
      buttonText: 'Post now',
      // Pull to testing posts it; a drag on the board waits for the watcher
      // sweep, which nothing here can vouch for.
      hint: `Pull to testing posts it to Jira automatically. Moving the card on the board relies on the watcher sweep.`,
    }
  }
  return { ...UNKNOWN, mode: 'unposted' }
}
