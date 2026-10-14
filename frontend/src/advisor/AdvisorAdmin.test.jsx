import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AdvisorAdmin } from './AdvisorAdmin'
import { advisorRequest } from './advisorApi'

vi.mock('./advisorApi', () => ({
  AdvisorError: class AdvisorError extends Error {},
  advisorRequest: vi.fn(),
}))

const auth = { access_token: 'portal-token', user: { id: 'admin-1', email: 'admin@cs.qau.edu.pk', role: 'admin' } }

describe('AdvisorAdmin account', () => {
  beforeEach(() => {
    sessionStorage.clear()
    advisorRequest.mockImplementation((path) => {
      if (path === '/admin/stats?days=7') return Promise.resolve({
        outcomes: {}, total: 0, latency_ms: {}, feedback: { up: 0, down: 0 }, daily: [], providers: {}, languages: {},
      })
      return Promise.resolve({})
    })
  })
  afterEach(() => { cleanup(); vi.clearAllMocks(); sessionStorage.clear() })

  it('changes a portal SSO password through the portal callback', async () => {
    const onPortalPasswordChange = vi.fn().mockResolvedValue({ password_changed: true })
    const onPortalLogout = vi.fn().mockResolvedValue(undefined)
    render(<AdvisorAdmin auth={auth} onPortalPasswordChange={onPortalPasswordChange} onPortalLogout={onPortalLogout} />)

    fireEvent.click(await screen.findByRole('tab', { name: 'Account' }))
    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'old-password' } })
    fireEvent.change(screen.getByLabelText(/New password \(10\+ characters\)/), { target: { value: 'new-password-123' } })
    fireEvent.change(screen.getByLabelText('Confirm new password'), { target: { value: 'new-password-123' } })
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))

    await waitFor(() => expect(onPortalPasswordChange).toHaveBeenCalledWith({
      current_password: 'old-password', new_password: 'new-password-123', confirm_password: 'new-password-123',
    }))
    expect(onPortalLogout).toHaveBeenCalledOnce()
    expect(advisorRequest).not.toHaveBeenCalledWith('/admin/password', expect.anything(), expect.anything())
  })
})
