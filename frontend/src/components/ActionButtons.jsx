/**
 * Buttons for test plan generation and bug analysis actions.
 */

import { Btn, Prog } from './ui'

function ActionButtons({
  onGenerateTestPlan,
  onStopGeneration,
  generatingPlan,
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
