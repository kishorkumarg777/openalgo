import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { readProfileSettings } from '@/lib/trading/profileSettings'
import type { OrderFlowUiState } from '@/lib/trading/terminal'
import { OrderFlowBar } from './OrderFlowBar'

function makeState(patch: Partial<OrderFlowUiState> = {}): OrderFlowUiState {
  return {
    settings: readProfileSettings('order-flow', {}),
    tickSize: 0.05,
    effectiveRowTicks: 5,
    effectiveRowSize: 0.25,
    recordedBars: 0,
    totalBars: 120,
    tradeCount: 0,
    hydratedTrades: 0,
    recording: true,
    syncing: false,
    ...patch,
  }
}

describe('OrderFlowBar', () => {
  it('shows active Auto precision, quick toggles, and OHLCV estimate badge', () => {
    const onPatch = vi.fn()
    const onSync = vi.fn()
    const onOpenSettings = vi.fn()

    render(
      <OrderFlowBar
        state={makeState()}
        onPatch={onPatch}
        onSync={onSync}
        onOpenSettings={onOpenSettings}
      />
    )

    expect(screen.getByRole('button', { name: /Row precision: Auto \(0\.25\)/i })).toBeTruthy()
    expect(screen.getByText('OHLCV Est.')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Table' }))
    expect(onPatch).toHaveBeenCalledWith({ 'profiles.of.showSummaryTable': true })

    fireEvent.click(screen.getByRole('button', { name: 'Zones' }))
    expect(onPatch).toHaveBeenCalledWith({ 'profiles.of.showZones': true })

    fireEvent.click(screen.getByRole('button', { name: /Sync recorded order flow tape/i }))
    expect(onSync).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Order Flow settings' }))
    expect(onOpenSettings).toHaveBeenCalledTimes(1)
  })

  it('displays live/recorded tape fidelity count when recorded prints exist', () => {
    render(
      <OrderFlowBar
        state={makeState({ recordedBars: 12, tradeCount: 2450 })}
        onPatch={vi.fn()}
        onSync={vi.fn()}
        onOpenSettings={vi.fn()}
      />
    )

    expect(screen.getByText('Tape: 12 bars · 2.5K')).toBeTruthy()
  })
})
