/**
 * Helper functions for state management
 */

export const initialTestingContext = {
  acceptanceCriteria: '',
  testDataNotes: '',
  environments: '',
  rolesPermissions: '',
  outOfScope: '',
  riskAreas: ''
}

export const resetTestingContext = () => ({ ...initialTestingContext })
