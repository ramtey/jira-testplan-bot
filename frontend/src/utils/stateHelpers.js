/**
 * Helper functions for state management
 */

export const initialTestingContext = {
  acceptanceCriteria: '',
  testDataNotes: '',
  environments: '',
  rolesPermissions: '',
  outOfScope: '',
  riskAreas: '',
  specialInstructions: ''
}

export const resetTestingContext = () => ({ ...initialTestingContext })
