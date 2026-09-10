// Does the plan on screen predate the code it describes?
//
// The queue watcher writes a plan the moment a ticket lands in the QA queue and
// never regenerates it — deliberately, so unattended sweeps can't re-spend on a
// ticket forever. Two guards keep it from writing too early (a ticket on hold is
// skipped, and at least one PR must be merged), but neither can help once the
// plan exists: a PR reopened and re-merged, or a second PR landing after the
// first, moves the code out from under a plan that then sits there looking
// authoritative. This is the read-side check that makes that visible.
//
// Deliberately compares against merge time only. An open PR is code still in
// flight and says nothing about whether the plan is stale; a merged one is the
// diff QA will actually be testing against.

/**
 * PRs merged strictly after `planCreatedAt`, newest first.
 *
 * Returns an empty array when the plan has no known creation time — a plan
 * generated in this session is new by definition, and guessing "now" would
 * make every fresh plan momentarily suspect.
 */
export function mergesAfterPlan(planCreatedAt, pullRequests) {
  if (!planCreatedAt || !Array.isArray(pullRequests)) return []
  const planMs = new Date(planCreatedAt).getTime()
  if (Number.isNaN(planMs)) return []

  return pullRequests
    .filter((pr) => {
      if (!pr?.merged_at) return false
      const mergedMs = new Date(pr.merged_at).getTime()
      return !Number.isNaN(mergedMs) && mergedMs > planMs
    })
    .sort((a, b) => new Date(b.merged_at).getTime() - new Date(a.merged_at).getTime())
}
