/**
 * Buttons for test plan generation and bug analysis actions.
 */

import { useEffect, useState } from 'react'
import { Btn, Prog } from './ui'
import { formatClock, formatDuration } from '../utils/time'

const LAST_RUN_LABEL = { done: 'Generated in', failed: 'Failed after', stopped: 'Stopped after' }

// Ticks once a second from `startedAt` (epoch ms). Its own component so the
// second-by-second re-render stays out of the rest of the action row.
function ElapsedClock({ startedAt }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [startedAt])
  return (
    <span className="tnum" title="Time since you clicked Generate" style={{ color: 'var(--fg-muted)' }}>
      {formatClock(now - startedAt)}
    </span>
  )
}

function ActionButtons({
  pendingLabel,
  onGenerateTestPlan,
  onStopGeneration,
  generatingPlan,
  generationStartedAt,
  lastGeneration,
  onAnalyzeBug,
  onStopBugAnalysis,
  analyzingBug,
  showBugLens,
  bugAnalysisAutoTriggered,
  bugAnalysisComplete,
}) {
  const isBusy = generatingPlan || analyzingBug
  const busyLabel = generatingPlan
    ? 'Generating test plan…'
    : bugAnalysisAutoTriggered
      ? 'Auto-analyzing after test plan…'
      : 'Analyzing bug…'
  const showAutoRunCaption =
    showBugLens && bugAnalysisAutoTriggered && bugAnalysisComplete && !isBusy

  // The ticket is still loading, or the app hasn't finished checking whether a
  // plan already exists. Clicking Generate now would run against partial data
  // or duplicate a plan that's about to appear, so show what's outstanding
  // instead of a button. A run already in flight outranks this — its Stop
  // control has to stay reachable.
  if (pendingLabel && !isBusy) {
    return (
      <div style={{ marginTop: 'var(--s-6)' }}>
        <span
          role="status"
          aria-live="polite"
          style={{
            color: 'var(--fg-subtle)',
            fontSize: 'var(--t-sm)',
            display: 'inline-flex',
            alignItems: 'center',
            gap: 'var(--s-3)',
          }}
        >
          <span className="spin" style={{ color: 'var(--accent)' }} />
          {pendingLabel}
        </span>
      </div>
    )
  }

  return (
    <div style={{ marginTop: 'var(--s-6)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', flexWrap: 'wrap' }}>
        {!generatingPlan ? (
          <Btn
            variant="primary"
            icon="beaker"
            onClick={() => onGenerateTestPlan()}
            disabled={analyzingBug}
          >
            Generate test plan
          </Btn>
        ) : (
          <Btn variant="danger-soft" icon="stop" onClick={onStopGeneration}>
            Stop generation
          </Btn>
        )}

        {showBugLens && (
          !analyzingBug ? (
            <Btn
              variant="secondary"
              icon="scan"
              onClick={onAnalyzeBug}
              disabled={isBusy}
            >
              Analyze with Bug Lens
            </Btn>
          ) : (
            <Btn variant="danger-soft" icon="stop" onClick={onStopBugAnalysis}>
              Stop analysis
            </Btn>
          )
        )}

        {isBusy && (
          <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-sm)', marginLeft: 'var(--s-3)', display: 'inline-flex', alignItems: 'center', gap: 'var(--s-3)' }}>
            <span className="spin" style={{ color: 'var(--accent)' }} />
            {busyLabel}
            {generatingPlan && generationStartedAt && (
              <>
                <span aria-hidden="true">·</span>
                <ElapsedClock startedAt={generationStartedAt} />
              </>
            )}
          </span>
        )}

        {!isBusy && lastGeneration && (
          <span
            className="tnum"
            style={{
              color: lastGeneration.outcome === 'done' ? 'var(--fg-subtle)' : 'var(--fg-muted)',
              fontSize: 'var(--t-sm)',
              marginLeft: 'var(--s-3)',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 'var(--s-2)',
            }}
          >
            <span aria-hidden="true">⏱</span>
            {LAST_RUN_LABEL[lastGeneration.outcome]} {formatDuration(lastGeneration.ms)}
          </span>
        )}

        {showAutoRunCaption && (
          <span
            title="Bug Lens ran automatically the first time this ticket entered In Testing."
            style={{
              color: 'var(--fg-subtle)',
              fontSize: 'var(--t-sm)',
              marginLeft: 'var(--s-3)',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 'var(--s-2)',
            }}
          >
            <span aria-hidden="true">·</span>
            Auto-run after test plan
          </span>
        )}
      </div>

      {isBusy && (
        <div style={{ marginTop: 'var(--s-5)' }}>
          <Prog indeterminate />
        </div>
      )}
    </div>
  )
}

export default ActionButtons
