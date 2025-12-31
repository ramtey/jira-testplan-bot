/**
 * Form for additional testing context input
 */

function TestingContextForm({
  ticketData,
  testingContext,
  onContextChange,
  onGenerateTestPlan,
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
      </div>

      <div className="generate-section">
        <button
          type="button"
          onClick={onGenerateTestPlan}
          disabled={generatingPlan}
          className="btn-generate"
        >
          {generatingPlan ? (
            <>
              <span className="spinner"></span>
              Generating Test Plan<span className="dots"></span>
            </>
          ) : (
            'Generate Test Plan'
          )}
        </button>
        {generatingPlan && (
          <p className="generation-message">
            This may take 30-120 seconds. Please wait...
          </p>
        )}
      </div>
    </div>
  )
}

export default TestingContextForm
