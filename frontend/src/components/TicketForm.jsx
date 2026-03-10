/**
 * Form for fetching Jira tickets
 */

function TicketForm({ issueKey, setIssueKey, loading, onSubmit, onClear, hasTicketData }) {
  const isMulti = issueKey.includes(',')

  return (
    <form onSubmit={onSubmit} className="ticket-form">
      <div className="form-group">
        <label htmlFor="issueKey">Jira Issue Key(s)</label>
        <div className="input-group">
          <input
            id="issueKey"
            type="text"
            placeholder="e.g., PROJ-123 or PROJ-123, PROJ-456"
            value={issueKey}
            onChange={(e) => setIssueKey(e.target.value)}
            disabled={loading}
            autoComplete="off"
          />
          <button type="submit" disabled={loading}>
            {loading ? 'Fetching...' : isMulti ? 'Fetch Tickets' : 'Fetch Ticket'}
          </button>
          {hasTicketData && (
            <button type="button" onClick={onClear} className="btn-clear">
              Clear
            </button>
          )}
        </div>
      </div>
    </form>
  )
}

export default TicketForm
