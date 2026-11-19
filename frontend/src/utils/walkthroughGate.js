// Copy for the Pass-to-UAT override prompt, keyed by the server's
// `walkthrough_reason` (src/app/repositories/walkthrough_repository.py).
//
// The server decides whether a walkthrough is needed; this only words the
// prompt. A backend-only ticket (every case an API or terminal check) never
// gets the 409, so it never reaches here. A plan with manual checks but no UI
// cases is asked for written evidence, not a video, and is told so.

const EVIDENCE_REASONS = new Set(['manual_checks_need_evidence', 'attachments_unchecked'])

export function walkthroughPromptCopy(reason) {
  if (EVIDENCE_REASONS.has(reason)) {
    return {
      title: 'Pass to UAT without evidence?',
      sub: 'This ticket is flagged high-complexity and its plan has manual checks but no UI cases. Notes on the walkthrough card or a results file attached in Jira are enough.',
    }
  }
  return {
    title: 'Pass to UAT without a walkthrough?',
    sub: 'This ticket is flagged high-complexity — testers usually need a video, screenshot, or notes to run it.',
  }
}
