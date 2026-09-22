import { etDateTime } from '../lib/format'
import type { PlanDetailResponse } from '../lib/types'
import { Pill } from './Pill'

export function RunbookBanner({
  d,
  compact = false,
}: {
  d: PlanDetailResponse
  compact?: boolean
}) {
  const rb = d.runbook
  return (
    <section className="card">
      <div className="card-head">
        <div>
          <div className="eyebrow">live runbook</div>
          <h2 className="banner-title">
            {rb.monitor_armed ? (
              <Pill variant="armed">● armed</Pill>
            ) : rb.heartbeat ? (
              <Pill variant="disarmed">● disarmed — heartbeat stale</Pill>
            ) : (
              <Pill variant="empty">○ not armed</Pill>
            )}
          </h2>
          {!compact && (
            <p className="muted banner-sub">
              Current ET: <code>{etDateTime(rb.now_et)}</code> · entry date{' '}
              <code>{rb.entry_date}</code>
            </p>
          )}
        </div>
        <div className="pill-col">
          {rb.window_state === 'during' ? (
            <Pill variant="armed">● entry window open</Pill>
          ) : rb.window_state === 'before' ? (
            <Pill variant="empty">
              ○ window opens {d.plan.entry_window_start} ET
            </Pill>
          ) : rb.window_state === 'after' ? (
            <Pill variant="disarmed">
              ● window closed {d.plan.entry_window_end} ET
            </Pill>
          ) : (
            <Pill variant="empty">○ entry date {rb.entry_date}</Pill>
          )}
          {d.gateway_reachable ? (
            <Pill variant="armed">● gateway :4002</Pill>
          ) : (
            <Pill variant="disarmed">○ gateway :4002 not reachable</Pill>
          )}
        </div>
      </div>
      {!d.state_present && (
        <p className="muted" style={{ marginBottom: 0 }}>
          No state yet — the monitor has not written book.json for this plan.
        </p>
      )}
    </section>
  )
}
