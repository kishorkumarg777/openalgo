import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export interface ScreenerConfigState {
  strategy: string
  timeframe: string
  scanOverrides: Record<string, unknown>
  strategyOverrides: Record<string, Record<string, unknown>>
  watchlistFileName: string

  setStrategy: (strategy: string, allowedTimeframes?: string[]) => void
  setTimeframe: (timeframe: string) => void
  updateScan: (patch: Record<string, unknown>) => void
  setWatchlist: (fileName: string, symbolsText: string) => void
  updateStrategySetting: (strategyId: string, key: string, value: unknown) => void
  resetScan: () => void
  resetStrategy: (strategyId: string) => void
  resetAll: () => void
  importConfigJson: (raw: string) => { ok: boolean; error?: string }
}

export const useScreenerStore = create<ScreenerConfigState>()(
  persist(
    (set) => ({
      strategy: 'smc_breaker',
      timeframe: '1D',
      scanOverrides: {},
      strategyOverrides: {},
      watchlistFileName: '',

      setStrategy: (strategy, allowedTimeframes) =>
        set((state) => {
          let nextTf = state.timeframe
          if (allowedTimeframes && allowedTimeframes.length > 0 && !allowedTimeframes.includes(nextTf)) {
            nextTf = allowedTimeframes.includes('1H') ? '1H' : allowedTimeframes[0]
          }
          return { strategy, timeframe: nextTf }
        }),

      setTimeframe: (timeframe) => set({ timeframe }),

      updateScan: (patch) =>
        set((state) => ({
          scanOverrides: { ...state.scanOverrides, ...patch },
        })),

      setWatchlist: (fileName, symbolsText) =>
        set((state) => ({
          watchlistFileName: fileName,
          scanOverrides: {
            ...state.scanOverrides,
            universe_mode: 'watchlist',
            watchlist_symbols: symbolsText,
          },
        })),

      updateStrategySetting: (strategyId, key, value) =>
        set((state) => ({
          strategyOverrides: {
            ...state.strategyOverrides,
            [strategyId]: {
              ...(state.strategyOverrides[strategyId] ?? {}),
              [key]: value,
            },
          },
        })),

      resetScan: () => set({ scanOverrides: {}, watchlistFileName: '' }),

      resetStrategy: (strategyId) =>
        set((state) => {
          const next = { ...state.strategyOverrides }
          delete next[strategyId]
          return { strategyOverrides: next }
        }),

      resetAll: () =>
        set({
          strategy: 'smc_breaker',
          timeframe: '1D',
          scanOverrides: {},
          strategyOverrides: {},
          watchlistFileName: '',
        }),

      importConfigJson: (raw) => {
        try {
          const parsed = JSON.parse(raw) as {
            scan?: Record<string, unknown>
            strategies?: Record<string, Record<string, unknown>>
          }
          if (!parsed || typeof parsed !== 'object') {
            return { ok: false, error: 'JSON root must be an object with "scan" and/or "strategies".' }
          }
          const scanObj = parsed.scan && typeof parsed.scan === 'object' ? { ...parsed.scan } : {}
          const stratsObj =
            parsed.strategies && typeof parsed.strategies === 'object' ? { ...parsed.strategies } : {}

          // Drop stock_screener desktop-only local file path if present
          delete scanObj.watchlist_path

          const nextStrategy = typeof scanObj.strategy === 'string' ? scanObj.strategy : undefined
          const nextTimeframe = typeof scanObj.timeframe === 'string' ? scanObj.timeframe : undefined
          delete scanObj.strategy
          delete scanObj.timeframe

          set((state) => ({
            strategy: nextStrategy ?? state.strategy,
            timeframe: nextTimeframe ?? state.timeframe,
            scanOverrides: { ...state.scanOverrides, ...scanObj },
            strategyOverrides: { ...state.strategyOverrides, ...stratsObj },
          }))
          return { ok: true }
        } catch (err) {
          return {
            ok: false,
            error: err instanceof Error ? err.message : 'Invalid JSON format.',
          }
        }
      },
    }),
    {
      name: 'openalgo-smc-screener-v1',
    }
  )
)
