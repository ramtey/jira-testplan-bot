/**
 * Form for test plan generation and bug analysis actions.
 */

function TestingContextForm({
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
    <div className="action-buttons">
      <div className="action-buttons-row">
        {/* Test Plan */}
        {!generatingPlan ? (
          <button
            type="button"
            onClick={onGenerateTestPlan}
            className="btn-generate"
            disabled={analyzingBug}
          >
            Generate Test Plan
          </button>
        ) : (
          <button type="button" onClick={onStopGeneration} className="btn-stop">
            <span className="spinner"></span>
            Stop Generation
          </button>
        )}

        {/* Bug Lens — only shown for Bug issue types */}
        {showBugLens && (!analyzingBug ? (
          <button
            type="button"
            onClick={onAnalyzeBug}
            className="btn-generate btn-bug-lens"
            disabled={isBusy}
          >
            Analyze Bug
          </button>
        ) : (
          <button type="button" onClick={onStopBugAnalysis} className="btn-stop btn-stop-bug">
            <span className="spinner"></span>
            Stop Analysis
          </button>
        ))}
      </div>

      {generatingPlan && (
        <p className="generation-message">
          Generating test plan<span className="dots"></span>
        </p>
      )}
      {analyzingBug && (
        <p className="generation-message">
          Analyzing bug<span className="dots"></span>
        </p>
      )}
    </div>
  )
}

export default TestingContextForm
