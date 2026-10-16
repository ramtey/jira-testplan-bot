/**
 * The header pill for the queue watcher, from GET /health/watcher.
 *
 * The server decides whether the watcher is late (src/app/main.py,
 * check_watcher); this only words it. Returns null when there is nothing to
 * say: a healthy watcher, one never set up, or a status that could not be
 * read — the last because the database being down already breaks everything
 * else on the page louder than this pill could.
 */
const RESTART = 'launchctl kickstart -k gui/$(id -u)/com.jira-testplan-bot.watch'
const LOG = '~/Library/Logs/jira-testplan-watch.log'

const ago = (minutes) => {
  if (minutes == null) return ''
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  return hours < 48 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`
}

export const watcherPill = (health) => {
  if (!health) return null
  if (health.state === 'late') {
    return {
      tone: 'danger',
      label: `Watcher stopped · ${ago(health.minutes_since)}`,
      detail:
        'No sweep has finished since then, so Ready to Test tickets are not getting plans and board moves to In Testing are not being posted. ' +
        `Restart it with: ${RESTART} (log: ${LOG})`,
    }
  }
  if (health.state === 'error') {
    return {
      tone: 'warn',
      label: 'Watcher can’t read Jira',
      detail: `The last sweep ran but could not read the queue: ${health.queue_error || 'unknown error'}. Log: ${LOG}`,
    }
  }
  return null
}
