/**
 * Display bug analysis results from Jira Bug Lens.
 */

import { formatBugAnalysisAsMarkdown } from '../utils/markdown'
import Icon from './Icon'
import { Btn, Chip } from './ui'

function Section({ icon, title, description, children, style }) {
  return (
    <section style={{ marginTop: 'var(--s-7)', ...style }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginBottom: 'var(--s-3)' }}>
        <Icon name={icon} size={14} style={{ color: 'var(--accent)' }} />
        <h3 style={{ margin: 0, fontSize: 'var(--t-md)', fontWeight: 600, color: 'var(--fg-strong)' }}>{title}</h3>
      </header>
      {description && (
        <p style={{ margin: '0 0 var(--s-3)', color: 'var(--fg-subtle)', fontSize: 'var(--t-sm)' }}>{description}</p>
      )}
      {children}
    </section>
  )
}

function BulletList({ items }) {
  return (
    <ul style={{ margin: 0, paddingLeft: 18, fontSize: 'var(--t-sm)', lineHeight: '20px', color: 'var(--fg)' }}>
      {items.map((item, i) => <li key={i}>{item}</li>)}
    </ul>
  )
}

function NumberedList({ items }) {
  return (
    <ol style={{ margin: 0, paddingLeft: 18, fontSize: 'var(--t-sm)', lineHeight: '20px', color: 'var(--fg)' }}>
      {items.map((item, i) => <li key={i}>{item.replace(/^\s*\d+[.)]\s+/, '')}</li>)}
    </ol>
  )
}

function FixStatusCard({ analysis, fixStatus }) {
  const tone = {
    fixed: { color: 'var(--success)', bg: 'rgba(34,197,94,.14)', icon: 'check-circle', label: 'Fixed' },
    in_testing: { color: 'var(--info)', bg: 'rgba(59,130,246,.14)', icon: 'beaker', label: 'In testing — awaiting QA' },
    not_fixed: { color: 'var(--warning)', bg: 'rgba(245,158,11,.14)', icon: 'alert', label: 'Not yet fixed' },
  }[fixStatus] || { color: 'var(--warning)', bg: 'rgba(245,158,11,.14)', icon: 'alert', label: 'Not yet fixed' }

  return (
    <div className="card" style={{ padding: 'var(--s-5)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
        <Icon name="shield" size={12} style={{ color: 'var(--fg-subtle)' }} />
        <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '.04em', fontWeight: 600 }}>Fix status</span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)' }}>
        <span style={{ width: 28, height: 28, borderRadius: 'var(--r-md)', background: tone.bg, display: 'grid', placeItems: 'center' }}>
          <Icon name={tone.icon} size={14} style={{ color: tone.color }} />
        </span>
        <div style={{ fontSize: 'var(--t-md)', fontWeight: 600, color: tone.color }}>{tone.label}</div>
      </div>
    </div>
  )
}

function OriginCard({ analysis }) {
  if (analysis.is_regression == null) return null
  const isReg = analysis.is_regression
  const color = isReg ? 'var(--warning)' : 'var(--info)'
  const bg = isReg ? 'rgba(245,158,11,.14)' : 'rgba(59,130,246,.14)'
  const icon = isReg ? 'history' : 'sparkles'
  return (
    <div className="card" style={{ padding: 'var(--s-5)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
        <Icon name="history" size={12} style={{ color: 'var(--fg-subtle)' }} />
        <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '.04em', fontWeight: 600 }}>Origin</span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)' }}>
        <span style={{ width: 28, height: 28, borderRadius: 'var(--r-md)', background: bg, display: 'grid', placeItems: 'center' }}>
          <Icon name={icon} size={14} style={{ color }} />
        </span>
        <div>
          <div style={{ fontSize: 'var(--t-md)', fontWeight: 600, color }}>{isReg ? 'Regression' : 'Never worked'}</div>
          {isReg && analysis.regression_introduced_by && (
            <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>Introduced by {analysis.regression_introduced_by}</div>
          )}
        </div>
      </div>
    </div>
  )
}

function BugAnalysisDisplay({ analysis }) {
  const isMulti = Array.isArray(analysis.ticket_keys)
  const allKeys = isMulti ? analysis.ticket_keys : [analysis.ticket_key]
  const fixStatus = analysis.fix_status || (analysis.is_fixed ? 'fixed' : 'not_fixed')

  const handleDownloadMarkdown = () => {
    const markdown = formatBugAnalysisAsMarkdown(analysis)
    const blob = new Blob([markdown], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = isMulti
      ? `bug-analysis-${analysis.ticket_keys.join('-')}.md`
      : `bug-analysis-${analysis.ticket_key}.md`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  const codeEvidenceWithHits = (analysis.code_evidence || []).filter(
    (e) => e.usages && e.usages.length > 0
  )
  const codeEvidenceMissed = (analysis.code_evidence || []).length > 0 && codeEvidenceWithHits.length === 0

  return (
    <div style={{ marginTop: 'var(--s-7)' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-4)', marginBottom: 'var(--s-5)' }}>
        <Icon name="scan" size={16} style={{ color: 'var(--warning)' }} />
        <h2 style={{ margin: 0, fontSize: 'var(--t-xl)', fontWeight: 600, color: 'var(--fg-strong)' }}>Bug Lens Analysis</h2>
        <Chip>{allKeys.join(' + ')}</Chip>
        <span style={{ flex: 1 }} />
        <Btn variant="ghost" size="sm" icon="download" onClick={handleDownloadMarkdown}>Download .md</Btn>
      </div>

      {/* Status row */}
      <div style={{ display: 'grid', gridTemplateColumns: analysis.is_regression != null ? '1fr 1fr' : '1fr', gap: 'var(--s-4)', marginBottom: 'var(--s-6)' }}>
        <FixStatusCard analysis={analysis} fixStatus={fixStatus} />
        <OriginCard analysis={analysis} />
      </div>

      {analysis.bug_summary && (
        <Section icon="info" title="Bug Summary">
          <p style={{ margin: 0, color: 'var(--fg)' }}>{analysis.bug_summary}</p>
        </Section>
      )}

      <Section icon="alert-circle" title="Root Cause">
        {analysis.root_cause ? (
          <p style={{ margin: 0, color: 'var(--fg)' }}>{analysis.root_cause}</p>
        ) : (
          <p style={{ margin: 0, color: 'var(--fg-subtle)' }}>No code diff available — root cause derived from ticket description only.</p>
        )}
      </Section>

      {analysis.affected_flow && analysis.affected_flow.length > 0 && (
        <Section icon="arrow-right" title="Affected Flow" description="End-to-end path from user action to the bug:">
          <NumberedList items={analysis.affected_flow} />
        </Section>
      )}

      {analysis.scope_of_impact && analysis.scope_of_impact.length > 0 && (
        <Section icon="layers" title="Scope of Impact" description="Other features or callers affected by the same broken code:">
          <BulletList items={analysis.scope_of_impact} />
        </Section>
      )}

      {codeEvidenceMissed && (
        <Section icon="code" title="Code Evidence">
          <p style={{ margin: 0, color: 'var(--fg-subtle)' }}>
            Searched for the suspected symbols but none were found in the candidate repos — the bug may live in a different repo, or the suspects were off. Verify before acting.
          </p>
        </Section>
      )}

      {codeEvidenceWithHits.length > 0 && (
        <Section icon="code" title="Code Evidence" description="Places the suspected symbols actually appear in the repo — verify before acting.">
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s-4)' }}>
            {codeEvidenceWithHits.map((entry, i) => (
              <div key={i} className="codeblock">
                <div className="head">
                  <code style={{ color: 'var(--fg-strong)' }}>{entry.suspect}</code>
                  <span className="ext">in {entry.repo}</span>
                </div>
                <ul style={{ margin: 0, padding: 'var(--s-4) var(--s-5)', listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 'var(--s-3)' }}>
                  {entry.usages.map((u, j) => (
                    <li key={j}>
                      <a
                        href={encodeURI(`https://github.com/${entry.repo}/blob/${u.ref}/${u.path}`) + `#L${u.line}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{ color: 'var(--accent)', fontFamily: 'var(--font-mono)', fontSize: 11.5 }}
                      >
                        {u.path}:{u.line} <Icon name="external" size={10} style={{ verticalAlign: -1 }} />
                      </a>
                      {u.snippet && (
                        <pre style={{ margin: '6px 0 0', fontSize: 11.5, lineHeight: '17px', color: 'var(--fg)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                          {u.snippet}
                        </pre>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </Section>
      )}

      {analysis.why_tests_miss && (
        <Section icon="bug" title="Why Tests Don't Catch This">
          <p style={{ margin: 0, color: 'var(--fg)' }}>{analysis.why_tests_miss}</p>
        </Section>
      )}

      {(fixStatus === 'fixed' || fixStatus === 'in_testing') && (
        <Section icon="shield" title="Fix Explanation">
          {analysis.fix_explanation ? (
            <p style={{ margin: 0, color: 'var(--fg)' }}>{analysis.fix_explanation}</p>
          ) : (
            <p style={{ margin: 0, color: 'var(--fg-subtle)' }}>No fix details available.</p>
          )}
          {fixStatus === 'in_testing' && (
            <p style={{ marginTop: 'var(--s-3)', color: 'var(--fg-subtle)', fontSize: 'var(--t-sm)' }}>
              The code change is in but QA hasn't validated it yet — confirm the fix behaves correctly before closing the ticket.
            </p>
          )}
        </Section>
      )}

      {analysis.open_questions && analysis.open_questions.length > 0 && (
        <Section icon="help" title="Open Questions" description="Resolve these before committing to an estimate or fix:">
          <BulletList items={analysis.open_questions} />
        </Section>
      )}

      {analysis.assumptions && analysis.assumptions.length > 0 && (
        <Section icon="info" title="Assumptions" description="Inferences not directly grounded in the evidence — verify before acting:">
          <BulletList items={analysis.assumptions} />
        </Section>
      )}

      {fixStatus === 'not_fixed' && analysis.fix_complexity && (
        <Section icon="layers" title="Fix Complexity">
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginBottom: analysis.fix_complexity_reasoning ? 'var(--s-3)' : 0 }}>
            <Chip dot dotColor={
              analysis.fix_complexity === 'low' ? 'var(--success)' :
              analysis.fix_complexity === 'medium' ? 'var(--warning)' :
              'var(--danger)'
            }>
              {analysis.fix_complexity.charAt(0).toUpperCase() + analysis.fix_complexity.slice(1)}
            </Chip>
            {analysis.fix_effort_estimate && (
              <span style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)' }}>{analysis.fix_effort_estimate}</span>
            )}
          </div>
          {analysis.fix_complexity_reasoning && (
            <p style={{ margin: 0, color: 'var(--fg)', fontSize: 'var(--t-sm)' }}>{analysis.fix_complexity_reasoning}</p>
          )}
        </Section>
      )}

      {analysis.regression_tests && analysis.regression_tests.length > 0 && (
        <Section icon="beaker" title="Regression Tests" description="Run these to confirm the bug does not recur:">
          <BulletList items={analysis.regression_tests} />
        </Section>
      )}

      {analysis.similar_patterns && analysis.similar_patterns.length > 0 && (
        <Section icon="layers" title="Similar Bug Patterns to Watch" description="Related classes of bugs that may exist elsewhere in the codebase:">
          <BulletList items={analysis.similar_patterns} />
        </Section>
      )}

      <div style={{ marginTop: 'var(--s-8)', display: 'flex', justifyContent: 'flex-end' }}>
        <Btn variant="ghost" icon="download" onClick={handleDownloadMarkdown}>Download as .md</Btn>
      </div>
    </div>
  )
}

export default BugAnalysisDisplay
