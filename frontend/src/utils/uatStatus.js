/**
 * The header pill for unattended UAT runs, from GET /health/uat.
 *
 * The server decides whether the dispatcher is late (src/app/services/
 * uat_dispatch.py, health); this only words it. A stopped or failing
 * dispatcher wins over the review count: runs not starting is the thing
 * nobody would otherwise notice. Null when there is nothing to say.
 */
const RESTART = 'launchctl kickstart -k gui/$(id -u)/com.jira-testplan-bot.uat-dispatch'
const LOG = '~/Library/Logs/jira-testplan-uat-dispatch.log'

const ago = (minutes) => {
  if (minutes == null) return ''
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  return hours < 48 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`
}

export const uatPill = (health) => {
  if (!health) return null
  if (health.state === 'late') {
    return {
      tone: 'danger',
      label: `UAT dispatcher stopped · ${ago(health.minutes_since)}`,
      detail: `No dispatch tick since then, so queued UAT runs are not starting. Restart it with: ${RESTART} (log: ${LOG})`,
      tickets: [],
    }
  }
  if (health.state === 'error') {
    return {
      tone: 'warn',
      label: 'UAT dispatcher error',
      detail: `The last tick failed: ${health.error || 'unknown error'}. Log: ${LOG}`,
      tickets: [],
    }
  }
  const review = health.awaiting_review || []
  if (review.length) {
    const keys = [...new Set(review.map((r) => r.ticket_key))]
    return {
      tone: 'warn',
      label: `${review.length} UAT run${review.length === 1 ? '' : 's'} to review`,
      detail: `Waiting for your review: ${keys.join(', ')}. Open the ticket to approve or reject.`,
      tickets: keys,
    }
  }
  const running = health.running || []
  if (running.length) {
    return {
      tone: 'ok',
      label: `UAT running · ${running[0].ticket_key}`,
      detail: `Run ${running[0].run_id} is running. ${health.queued || 0} queued, ${health.waiting_for_build || 0} waiting for a deploy.`,
      tickets: [],
    }
  }
  return null
}
