// Render an approved UAT run as its Jira results comment, for the backend.
//
// Reads the run's review view as JSON on stdin and writes the comment text to
// stdout. Same formatUatResultsAsJira the review panel previews with, so the
// posted comment is the one the reviewer read.
import { formatUatResultsAsJira } from '../src/utils/uatResults.js'

let input = ''
process.stdin.setEncoding('utf8')
for await (const chunk of process.stdin) input += chunk

process.stdout.write(formatUatResultsAsJira(JSON.parse(input)))
