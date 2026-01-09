/**
 * Display development information (commits, PRs, branches) for a Jira ticket
 */

function DevelopmentInfo({ developmentInfo }) {
  if (!developmentInfo) {
    return (
      <div className="ticket-section">
        <h3>Development Activity</h3>
        <p className="no-data">No linked commits, pull requests, or branches found</p>
        <p className="info-note">
          Development information will appear here when commits or pull requests are linked to this ticket.
        </p>
      </div>
    )
  }

  const hasCommits = developmentInfo.commits && developmentInfo.commits.length > 0
  const hasPullRequests = developmentInfo.pull_requests && developmentInfo.pull_requests.length > 0
  const hasBranches = developmentInfo.branches && developmentInfo.branches.length > 0

  if (!hasCommits && !hasPullRequests && !hasBranches) {
    return (
      <div className="ticket-section">
        <h3>Development Activity</h3>
        <p className="no-data">No development activity detected</p>
      </div>
    )
  }

  const getPRStatusClass = (status) => {
    const statusLower = status.toLowerCase()
    if (statusLower === 'merged' || statusLower === 'open') return 'success'
    if (statusLower === 'declined' || statusLower === 'closed') return 'warning'
    return ''
  }

  return (
    <div className="ticket-section">
      <h3>Development Activity</h3>

      {hasPullRequests && (
        <div className="dev-subsection">
          <h4>Pull Requests ({developmentInfo.pull_requests.length})</h4>
          <div className="dev-list">
            {developmentInfo.pull_requests.map((pr, index) => (
              <div key={index} className="dev-item pr-item">
                <div className="dev-item-header">
                  {pr.url ? (
                    <a href={pr.url} target="_blank" rel="noopener noreferrer" className="dev-link">
                      {pr.title}
                    </a>
                  ) : (
                    <span className="dev-title">{pr.title}</span>
                  )}
                  <span className={`pr-status ${getPRStatusClass(pr.status)}`}>
                    {pr.status}
                  </span>
                </div>
                {(pr.source_branch || pr.destination_branch) && (
                  <div className="pr-branches">
                    {pr.source_branch && (
                      <span className="branch-info">
                        <span className="branch-label">From:</span>
                        <code className="branch-name">{pr.source_branch}</code>
                      </span>
                    )}
                    {pr.destination_branch && (
                      <span className="branch-info">
                        <span className="branch-label">To:</span>
                        <code className="branch-name">{pr.destination_branch}</code>
                      </span>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {hasCommits && (
        <div className="dev-subsection">
          <h4>Commits</h4>
          <div className="commit-summary">
            <span className="commit-count">{developmentInfo.commits.length} commit{developmentInfo.commits.length !== 1 ? 's' : ''} linked to this ticket</span>
          </div>
        </div>
      )}

      {hasBranches && (
        <div className="dev-subsection">
          <h4>Branches ({developmentInfo.branches.length})</h4>
          <div className="branches-list">
            {developmentInfo.branches.map((branch, index) => (
              <code key={index} className="branch-tag">{branch}</code>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

export default DevelopmentInfo
