import { useTween } from '../hooks/useTween'
import { usd, usdSigned } from '../lib/format'
import type { AccountBlock, PortfolioBlock } from '../lib/types'
import { Pill } from './Pill'
import { Tile } from './StatTiles'

function freshness(seconds: number | null, stale: boolean): string {
  if (seconds === null) return '○ no marks'
  if (stale) return `● marks ${Math.round(seconds / 60)}m ago`
  return `● marks ${seconds}s ago`
}

/** Portfolio-level tiles: the whole paper book + the account equity.
 * Staleness pills derive from payload ages (transport staleness is a
 * different signal and lives in the header). */
export function PortfolioTiles({
  portfolio,
  account,
}: {
  portfolio: PortfolioBlock | null
  account: AccountBlock | null
}) {
  const openTween = useTween(portfolio?.unrealized_open ?? 0)
  if (!portfolio) return null
  return (
    <>
      <div className="pill-row" style={{ marginBottom: 10 }}>
        <Pill variant={portfolio.marks_stale ? 'empty' : 'armed'}>
          {freshness(portfolio.marks_age_seconds, portfolio.marks_stale)}
        </Pill>
        {account && (
          <Pill variant={account.age_seconds !== null && account.age_seconds > 180 ? 'empty' : 'armed'}>
            ● account{' '}
            {account.age_seconds !== null ? `${account.age_seconds}s ago` : 'fresh'}
          </Pill>
        )}
      </div>
      <div className="tiles">
        <Tile label="Open contracts" value={String(portfolio.open_qty)} />
        <Tile label="Committed (fills)" value={usd(portfolio.committed_filled)} />
        {portfolio.unrealized_open === null ? (
          <Tile label="Unrealized (open)" value="—" />
        ) : (
          <Tile
            label="Unrealized (open)"
            value={usdSigned(openTween)}
            className={portfolio.unrealized_open >= 0 ? 'pnl-pos' : 'pnl-neg'}
          />
        )}
        <Tile
          label="Realized to date"
          value={portfolio.realized === null ? '—' : usdSigned(portfolio.realized)}
          className={portfolio.realized !== null && portfolio.realized < 0 ? 'pnl-neg' : 'pnl-pos'}
        />
        {account && (
          <>
            <Tile
              label={`Net liq (${account.account_id})`}
              value={usd(account.net_liquidation)}
            />
            <Tile label={`Cash (${account.currency})`} value={usd(account.cash)} />
            <Tile label="Buying power" value={usd(account.buying_power)} />
          </>
        )}
      </div>
    </>
  )
}
