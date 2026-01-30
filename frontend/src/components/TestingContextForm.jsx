/**
 * Form for test plan generation
 */

function TestingContextForm({
  onGenerateTestPlan,
  onStopGeneration,
  generatingPlan
}) {
  return (
    <>
      {!generatingPlan ? (
        <button
          type="button"
          onClick={onGenerateTestPlan}
          className="btn-generate"
        >
          Generate Test Plan
        </button>
      ) : (
        <>
          <button
            type="button"
            onClick={onStopGeneration}
            className="btn-stop"
          >
            <span className="spinner"></span>
            Stop Generation
          </button>
          <p className="generation-message">
            Generating test plan<span className="dots"></span>
          </p>
        </>
      )}
    </>
  )
}

export default TestingContextForm
