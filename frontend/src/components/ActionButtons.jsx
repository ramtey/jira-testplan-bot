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
}) {
  const isBusy = generatingPlan || analyzingBug

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
            {generatingPlan ? 'Generating test plan…' : 'Analyzing bug…'}
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
