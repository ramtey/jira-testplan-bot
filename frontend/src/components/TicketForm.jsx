/**
 * Command-bar style ticket key input.
 */

import Icon from './Icon'
import { Btn } from './ui'

function TicketForm({ issueKey, setIssueKey, loading, onSubmit, onClear, hasTicketData }) {
  const isMulti = issueKey.includes(',')

  return (
    <form onSubmit={onSubmit}>
      <div className="inp-group" style={{ height: 44 }}>
        <span className="prefix" style={{ paddingLeft: 14, paddingRight: 10, color: 'var(--fg-faint)' }}>
          <Icon name="search" size={15} />
        </span>
        <input
          id="issueKey"
          type="text"
          className="inp mono"
          style={{ fontSize: 14, height: '100%', background: 'transparent', border: 0, paddingLeft: 0 }}
          placeholder="SK-2146   or   SK-2146, SK-2145, AP-318"
          value={issueKey}
          onChange={(e) => setIssueKey(e.target.value)}
          disabled={loading}
          autoComplete="off"
        />
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, paddingRight: 4 }}>
          {hasTicketData && (
            <Btn variant="ghost" size="sm" onClick={onClear} type="button">
              Clear
            </Btn>
          )}
          <Btn
            type="submit"
            variant="primary"
            iconRight="arrow-right"
            disabled={loading || !issueKey.trim()}
          >
            {isMulti ? 'Fetch tickets' : 'Fetch'}
          </Btn>
        </div>
      </div>
    </form>
  )
}

export default TicketForm
