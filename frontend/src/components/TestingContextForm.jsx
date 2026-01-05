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
            rows="3"
          />
        </div>

        <div className="form-field">
          <label htmlFor="testDataNotes">Test Data Notes</label>
          <textarea
            id="testDataNotes"
            placeholder="e.g., Test accounts, roles, sample data needed"
            value={testingContext.testDataNotes}
            onChange={(e) => onContextChange('testDataNotes', e.target.value)}
            rows="3"
          />
        </div>

        <div className="form-field">
          <label htmlFor="environments">Environments</label>
          <textarea
            id="environments"
            placeholder="e.g., Staging/prod flags, feature flags, configuration notes"
            value={testingContext.environments}
            onChange={(e) => onContextChange('environments', e.target.value)}
            rows="2"
          />
        </div>

        <div className="form-field">
          <label htmlFor="rolesPermissions">Roles/Permissions</label>
          <textarea
            id="rolesPermissions"
            placeholder="e.g., Admin, user, guest - which roles need testing?"
            value={testingContext.rolesPermissions}
            onChange={(e) => onContextChange('rolesPermissions', e.target.value)}
            rows="2"
          />
        </div>

        <div className="form-field">
          <label htmlFor="outOfScope">Out of Scope / Assumptions</label>
          <textarea
            id="outOfScope"
            placeholder="e.g., What's explicitly not included in this change?"
            value={testingContext.outOfScope}
            onChange={(e) => onContextChange('outOfScope', e.target.value)}
            rows="2"
          />
        </div>

        <div className="form-field">
          <label htmlFor="riskAreas">Known Risk Areas / Impacted Modules</label>
          <textarea
            id="riskAreas"
            placeholder="e.g., Authentication flow, payment processing, data migration"
            value={testingContext.riskAreas}
            onChange={(e) => onContextChange('riskAreas', e.target.value)}
            rows="2"
          />
        </div>

        <div className="form-field">
          <label htmlFor="specialInstructions">Special Testing Instructions</label>
          <textarea
            id="specialInstructions"
            placeholder="e.g., 'Test with specific examples from each category listed above' or 'Generate at least one test case per keyword category'"
            value={testingContext.specialInstructions}
            onChange={(e) => onContextChange('specialInstructions', e.target.value)}
            rows="3"
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
