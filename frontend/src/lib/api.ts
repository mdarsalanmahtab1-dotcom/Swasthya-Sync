/**
 * Authenticated fetch for staff screens (doctor, nurse, admin).
 *
 * Adds `Authorization: Bearer <token>` from localStorage:
 *   - admin:  swasthya_admin_token
 *   - doctor: swasthya_doctor_auth (JSON from /api/doctor/login, has .token)
 *   - nurse / other staff: swasthya_staff_token (from /api/auth/staff/login)
 * On 401 the stale token is cleared. Screens without their own login form
 * (nurse triage) get a minimal staff login prompt and the request is retried once.
 * The kiosk patient flow never uses this — it stays login-free.
 */
import { getApiBaseUrl } from '../config';

export const ADMIN_TOKEN_KEY = 'swasthya_admin_token';
export const DOCTOR_AUTH_KEY = 'swasthya_doctor_auth';
export const STAFF_TOKEN_KEY = 'swasthya_staff_token';

function safeGet(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}

export function getStaffToken(): string | null {
  const admin = safeGet(ADMIN_TOKEN_KEY);
  if (admin) return admin;
  const doc = safeGet(DOCTOR_AUTH_KEY);
  if (doc) {
    try {
      const parsed = JSON.parse(doc);
      if (parsed?.token) return parsed.token as string;
    } catch { /* ignore */ }
  }
  return safeGet(STAFF_TOKEN_KEY);
}

export function clearStaffTokens(): void {
  try {
    localStorage.removeItem(ADMIN_TOKEN_KEY);
    localStorage.removeItem(STAFF_TOKEN_KEY);
    const doc = localStorage.getItem(DOCTOR_AUTH_KEY);
    if (doc) {
      // Keep the doctor profile but force a fresh login
      localStorage.removeItem(DOCTOR_AUTH_KEY);
    }
  } catch { /* ignore */ }
}

async function promptStaffLogin(): Promise<boolean> {
  if (typeof window === 'undefined') return false;
  const username = window.prompt('Staff login required.\nStaff ID / username:');
  if (!username) return false;
  const password = window.prompt('Password:');
  if (!password) return false;
  const res = await fetch(`${getApiBaseUrl()}/api/auth/staff/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    window.alert('Login failed.');
    return false;
  }
  const data = await res.json();
  try { localStorage.setItem(STAFF_TOKEN_KEY, data.token); } catch { /* ignore */ }
  return true;
}

/** Absolute URL helper: accepts "/api/..." or a full URL. */
function toUrl(path: string): string {
  return /^https?:\/\//.test(path) ? path : `${getApiBaseUrl()}${path}`;
}

export async function apiFetch(path: string, init: RequestInit = {}, _retried = false): Promise<Response> {
  const headers = new Headers(init.headers || {});
  const token = getStaffToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const res = await fetch(toUrl(path), { ...init, headers });
  if (res.status === 401 && !_retried) {
    const hadAdmin = !!safeGet(ADMIN_TOKEN_KEY);
    const hadDoctor = !!safeGet(DOCTOR_AUTH_KEY);
    clearStaffTokens();
    if (hadAdmin || hadDoctor) {
      // Admin / doctor screens have their own login form — reload to show it.
      window.location.reload();
      return res;
    }
    if (await promptStaffLogin()) return apiFetch(path, init, true);
  }
  return res;
}

/** Fetch a protected file (PDF, document image) and open it in a new tab. */
export async function openProtectedFile(path: string): Promise<void> {
  const res = await apiFetch(path);
  if (!res.ok) { window.alert('Could not open file.'); return; }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  window.open(url, '_blank');
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
