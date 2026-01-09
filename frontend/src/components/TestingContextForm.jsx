/**
 * Form for additional testing context input
 */

function TestingContextForm({
  ticketData,
  testingContext,
  onContextChange,
  onGenerateTestPlan,
  onStopGeneration,
  generatingPlan
}) {
  return (
    <div className="ticket-section">
      <h3>Additional Testing Context</h3>
      <p className="section-description">
        Provide supplemental information to improve test plan quality (all fields optional)
      </p>

      <div className="context-form">
        <div className="form-field">
          <label htmlFor="acceptanceCriteria">
            Acceptance Criteria
            {(!ticketData.description || ticketData.description_quality.is_weak) && (
              <span className="field-suggested"> (Recommended)</span>
            )}
          </label>
          <textarea
            id="acceptanceCriteria"
            placeholder="e.g., Given a user clicks 'Forgot Password', when they enter their email, then they receive a reset link"
            value={testingContext.acceptanceCriteria}
            onChange={(e) => onContextChange('acceptanceCriteria', e.target.value)}
            rows="4"
          />
        </div>

        <div className="form-field">
          <label htmlFor="specialInstructions">Special Testing Instructions</label>
          <textarea
            id="specialInstructions"
            placeholder="e.g., 'Test with specific examples from each category listed above' or 'Generate at least one test case per keyword category'"
            value={testingContext.specialInstructions}
            onChange={(e) => onContextChange('specialInstructions', e.target.value)}
            rows="4"
          />
        </div>
      </div>

      <div className="generate-section">
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
      </div>
    </div>
  )
}

export default TestingContextForm
