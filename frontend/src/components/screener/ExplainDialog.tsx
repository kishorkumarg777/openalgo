import {
  CheckCircle2,
  ExternalLink,
  Layers,
  Loader2,
  Microscope,
  Search,
  ShieldAlert,
  XCircle,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import {
  type ExplainResult,
  explainScreenerSymbol,
  extractScreenerError,
} from '@/api/screener'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { chartHref } from '@/lib/trading/chartLink'

interface ExplainDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  initialSymbol: string
  apiKey: string | null
  strategy: string
  timeframe: string
  effectiveScan: Record<string, unknown>
  effectiveStrategy: Record<string, unknown>
}

export function ExplainDialog({
  open,
  onOpenChange,
  initialSymbol,
  apiKey,
  strategy,
  timeframe,
  effectiveScan,
  effectiveStrategy,
}: ExplainDialogProps) {
  const [symbolInput, setSymbolInput] = useState(initialSymbol)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ExplainResult | null>(null)

  const runExplain = async (symToExplain: string) => {
    const clean = symToExplain.trim().toUpperCase()
    if (!clean) {
      setError('Enter an NSE stock symbol (e.g. RELIANCE, TCS, HBLENGINE).')
      return
    }
    if (!apiKey) {
      setError('No OpenAlgo API key found. Generate an API key in Settings -> API Key first.')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const data = await explainScreenerSymbol({
        apikey: apiKey,
        symbol: clean,
        strategy,
        timeframe,
        scan: effectiveScan,
        strategy_settings: effectiveStrategy,
      })
      setResult(data)
    } catch (err) {
      setResult(null)
      setError(extractScreenerError(err, 'Could not explain symbol'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (open) {
      setSymbolInput(initialSymbol)
      if (initialSymbol.trim()) {
        void runExplain(initialSymbol)
      } else {
        setResult(null)
        setError(null)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialSymbol])

  const allObs = result ? [...result.internal_obs, ...result.swing_obs] : []

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[88vh] max-w-3xl flex-col gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b border-border px-5 py-4">
          <DialogTitle className="flex items-center gap-2 text-base">
            <Microscope className="h-4 w-4 text-primary" />
            SMC X-Ray Explain Diagnostic
          </DialogTitle>
          <DialogDescription className="text-xs">
            Inspect exactly why a stock matched or was rejected, plus every active Order Block and
            valid Bullish Breaker (+BB) seen by the LuxAlgo SMC engine.
          </DialogDescription>
        </DialogHeader>

        {/* Search form */}
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void runExplain(symbolInput)
          }}
          className="flex items-center gap-2 border-b border-border bg-muted/20 px-5 py-3"
        >
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={symbolInput}
              onChange={(e) => setSymbolInput(e.target.value.toUpperCase())}
              placeholder="Enter NSE ticker (e.g. RELIANCE, BSOFT, HBLENGINE)"
              className="h-9 pl-8 font-mono text-sm uppercase"
            />
          </div>
          <Badge variant="outline" className="h-9 px-2.5 font-mono text-xs">
            {timeframe}
          </Badge>
          <Button type="submit" size="sm" disabled={loading} className="h-9 gap-1.5">
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Microscope className="h-3.5 w-3.5" />}
            Explain
          </Button>
        </form>

        <ScrollArea className="flex-1 px-5 py-4">
          {error && (
            <div className="flex items-start gap-2.5 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-600 dark:text-rose-400">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {loading && !result && (
            <div className="flex flex-col items-center justify-center gap-2 py-14 text-sm text-muted-foreground">
              <Loader2 className="h-6 w-6 animate-spin text-primary" />
              <span>Running SMC state analysis on {symbolInput || 'symbol'}...</span>
            </div>
          )}

          {!loading && !result && !error && (
            <div className="py-12 text-center text-sm text-muted-foreground">
              Type any NSE ticker above and click <strong>Explain</strong> to inspect its judged candle,
              rule verdicts, active OBs, and valid bullish breakers.
            </div>
          )}

          {result && (
            <div className="space-y-5 pb-4">
              {/* Symbol header & judged candle */}
              <div className="rounded-xl border border-border bg-card p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="text-lg font-bold tracking-tight">{result.symbol}</span>
                      <Badge variant="secondary" className="font-mono text-xs">
                        {result.timeframe}
                      </Badge>
                      <Badge variant="outline" className="text-[11px]">
                        {result.candles_count} candles
                      </Badge>
                    </div>
                    {result.name && (
                      <p className="mt-0.5 text-xs text-muted-foreground">{result.name}</p>
                    )}
                    <p className="mt-1 text-xs text-muted-foreground">
                      Screener: <span className="font-medium text-foreground">{result.strategy_name}</span>
                    </p>
                  </div>
                  <Button asChild size="sm" variant="outline" className="h-8 gap-1.5 text-xs">
                    <a
                      href={chartHref(result.symbol, 'NSE')}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      Open Chart &amp; Order Flow
                      <ExternalLink className="h-3.5 w-3.5" />
                    </a>
                  </Button>
                </div>

                <div className="mt-3 grid grid-cols-2 gap-2 rounded-lg border border-border/70 bg-muted/25 p-3 text-xs sm:grid-cols-4">
                  <div>
                    <div className="text-[10px] uppercase text-muted-foreground">Candle Judged</div>
                    <div className="mt-0.5 font-mono font-semibold">{result.judged_candle.label}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase text-muted-foreground">O / H / L / C</div>
                    <div className="mt-0.5 font-mono tabular-nums">
                      {result.judged_candle.o} / {result.judged_candle.h} / {result.judged_candle.l} /{' '}
                      <span className="font-semibold text-foreground">{result.judged_candle.c}</span>
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase text-muted-foreground">Wilder RSI</div>
                    <div className="mt-0.5 font-mono font-semibold tabular-nums">
                      {result.judged_candle.rsi ?? 'n/a'}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase text-muted-foreground">Avg Vol / Value</div>
                    <div className="mt-0.5 font-mono tabular-nums">
                      {result.judged_candle.avg_volume !== null
                        ? result.judged_candle.avg_volume.toLocaleString('en-IN')
                        : 'n/a'}{' '}
                      {result.judged_candle.avg_value_cr !== null
                        ? `(${result.judged_candle.avg_value_cr} Cr)`
                        : ''}
                    </div>
                  </div>
                </div>
              </div>

              {/* Screener verdicts */}
              <div className="space-y-2">
                <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Screener Verdict
                </h4>
                {result.too_short ? (
                  <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-600 dark:text-amber-400">
                    Too few candles ({result.candles_count} available, need at least {result.min_candles}).
                  </div>
                ) : result.rows.length === 0 ? (
                  <div className="rounded-lg border border-border bg-muted/20 p-3 text-xs text-muted-foreground">
                    No candidate setup found on {result.symbol} ({result.timeframe}) for this screener.
                  </div>
                ) : (
                  <div className="space-y-2">
                    {result.rows.map((row, idx) => (
                      <div
                        key={idx}
                        className={`rounded-lg border p-3 text-xs ${
                          row.valid
                            ? 'border-emerald-500/30 bg-emerald-500/5'
                            : 'border-amber-500/30 bg-amber-500/5'
                        }`}
                      >
                        <div className="flex items-center gap-2 font-semibold">
                          {row.valid ? (
                            <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                          ) : (
                            <XCircle className="h-4 w-4 text-amber-500" />
                          )}
                          <Badge
                            variant="secondary"
                            className={
                              row.valid
                                ? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
                                : 'bg-amber-500/15 text-amber-600 dark:text-amber-400'
                            }
                          >
                            {row.valid ? 'MATCH' : 'REJECTED'}
                          </Badge>
                          <span>{String(row.values.status ?? '')}</span>
                        </div>
                        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-muted-foreground">
                          {Object.entries(row.values)
                            .filter(
                              ([k, v]) =>
                                !['symbol', 'status', 'timeframe'].includes(k) &&
                                v !== null &&
                                v !== undefined &&
                                v !== ''
                            )
                            .map(([k, v]) => (
                              <span key={k}>
                                <span className="text-foreground/70">{k}:</span>{' '}
                                <span className="text-foreground">{String(v)}</span>
                              </span>
                            ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Active Order Blocks */}
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <h4 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    <Layers className="h-3.5 w-3.5" />
                    Active Order Blocks ({allObs.length})
                  </h4>
                  <span className="text-[11px] text-muted-foreground">
                    Chart shows newest {String(result.chart_settings.chart_shown ?? 5)} per kind
                  </span>
                </div>
                {allObs.length === 0 ? (
                  <p className="rounded-lg border border-border bg-muted/20 p-3 text-xs text-muted-foreground">
                    No active order blocks found with the current chart settings.
                  </p>
                ) : (
                  <div className="overflow-x-auto rounded-lg border border-border">
                    <table className="w-full text-left text-xs">
                      <thead className="border-b border-border bg-muted/40 text-[11px] text-muted-foreground">
                        <tr>
                          <th className="px-3 py-2 font-medium">Structure</th>
                          <th className="px-3 py-2 font-medium">Bias</th>
                          <th className="px-3 py-2 font-medium">Zone (Low – High)</th>
                          <th className="px-3 py-2 font-medium">OB Candle</th>
                          <th className="px-3 py-2 font-medium">Formed</th>
                          <th className="px-3 py-2 font-medium">Chart</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border/60 font-mono">
                        {allObs.map((ob, idx) => (
                          <tr key={`${ob.kind}-${ob.rank}-${idx}`} className="hover:bg-muted/20">
                            <td className="px-3 py-1.5 font-sans">
                              {ob.kind} #{ob.rank}
                            </td>
                            <td className="px-3 py-1.5">
                              <Badge
                                variant="secondary"
                                className={
                                  ob.bias === 'bullish'
                                    ? 'bg-emerald-500/15 text-[10px] text-emerald-600 dark:text-emerald-400'
                                    : 'bg-rose-500/15 text-[10px] text-rose-600 dark:text-rose-400'
                                }
                              >
                                {ob.bias === 'bullish' ? 'Bullish OB' : 'Bearish OB'}
                              </Badge>
                            </td>
                            <td className="px-3 py-1.5 tabular-nums">
                              {ob.low.toFixed(2)} – {ob.high.toFixed(2)}
                            </td>
                            <td className="px-3 py-1.5 text-muted-foreground">{ob.ob_time}</td>
                            <td className="px-3 py-1.5 text-muted-foreground">{ob.formed_time}</td>
                            <td className="px-3 py-1.5 font-sans">
                              {ob.drawn_on_chart ? (
                                <span className="text-emerald-600 dark:text-emerald-400">Visible</span>
                              ) : (
                                <span className="text-muted-foreground">Hidden</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              {/* Valid Bullish Breakers (+BB) */}
              <div className="space-y-2">
                <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Valid Bullish Breakers (+BB) ({result.bullish_breakers.length})
                </h4>
                {result.bullish_breakers.length === 0 ? (
                  <p className="rounded-lg border border-border bg-muted/20 p-3 text-xs text-muted-foreground">
                    No valid bullish breaker blocks found.
                  </p>
                ) : (
                  <div className="overflow-x-auto rounded-lg border border-border">
                    <table className="w-full text-left text-xs">
                      <thead className="border-b border-border bg-muted/40 text-[11px] text-muted-foreground">
                        <tr>
                          <th className="px-3 py-2 font-medium">Zone (Low – High)</th>
                          <th className="px-3 py-2 font-medium">OB Candle</th>
                          <th className="px-3 py-2 font-medium">Breaker Formed</th>
                          <th className="px-3 py-2 font-medium">Source</th>
                          <th className="px-3 py-2 font-medium">Retest</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border/60 font-mono">
                        {result.bullish_breakers.map((bb, idx) => (
                          <tr key={idx} className="hover:bg-muted/20">
                            <td className="px-3 py-1.5 font-semibold tabular-nums text-sky-600 dark:text-sky-400">
                              {bb.low.toFixed(2)} – {bb.high.toFixed(2)}
                            </td>
                            <td className="px-3 py-1.5 text-muted-foreground">{bb.ob_time}</td>
                            <td className="px-3 py-1.5 text-muted-foreground">{bb.formed_time}</td>
                            <td className="px-3 py-1.5 font-sans">{bb.source}</td>
                            <td className="px-3 py-1.5 font-sans">
                              {bb.retested ? (
                                <span className="text-emerald-600 dark:text-emerald-400">
                                  Retested ({bb.retest_time})
                                </span>
                              ) : (
                                <span className="text-muted-foreground">Fresh</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          )}
        </ScrollArea>
      </DialogContent>
    </Dialog>
  )
}
