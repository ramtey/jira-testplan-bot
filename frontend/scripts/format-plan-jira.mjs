// Render a plan as its Jira comment, for the backend.
//
// Reads {"plan": ..., "walkthrough": ...} as JSON on stdin and writes the
// comment text to stdout. It calls the same formatTestPlanAsJira the Post to
// Jira button calls, so a plan posted automatically reads exactly like one
// posted by hand. A Python copy of the formatter would be a second producer,
// and the two would drift.
import { formatTestPlanAsJira } from '../src/utils/markdown.js'

let input = ''
process.stdin.setEncoding('utf8')
for await (const chunk of process.stdin) input += chunk

const { plan, walkthrough = null } = JSON.parse(input)
process.stdout.write(formatTestPlanAsJira(plan, walkthrough))
