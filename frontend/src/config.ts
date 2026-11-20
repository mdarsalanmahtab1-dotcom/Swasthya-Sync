/**
 * SwasthyaSync — Environment & API Configuration
 *
 * Dynamically resolves backend HTTP and WebSocket URLs based on current host/IP.
 * Production (Firebase Hosting) REQUIRES VITE_API_URL (the Cloud Run URL); the WS URL is
 * VITE_WS_URL or derived from VITE_API_URL. Localhost and LAN testing work without env vars.
 */

function missingProdBackend(): never {
  throw new Error('VITE_API_URL is not set for this production build (set it in frontend/.env.production).');
}

export function getApiBaseUrl(): string {
  if (import.meta.env.VITE_API_URL) {
    return (import.meta.env.VITE_API_URL as string).replace(/\/+$/, '');
  }
  if (import.meta.env.VITE_BACKEND_HTTP_URL) {
    return (import.meta.env.VITE_BACKEND_HTTP_URL as string).replace(/\/+$/, '');
  }
  
  if (typeof window !== 'undefined') {
    const hostname = window.location.hostname || 'localhost';
    const protocol = window.location.protocol;

    // Local development
    if (hostname === 'localhost' || hostname === '127.0.0.1') {
      return `${protocol}//localhost:8000`;
    }

    // Production builds must point at the backend explicitly via VITE_API_URL
    if (import.meta.env.PROD) {
      missingProdBackend();
    }

    // LAN / local network testing (e.g. phone accessing laptop on 192.168.x.x)
    return `${protocol}//${hostname}:8000`;
  }

  return 'http://localhost:8000';
}

export function getWsUrl(): string {
  if (import.meta.env.VITE_WS_URL) {
    return import.meta.env.VITE_WS_URL as string;
  }

  // If VITE_API_URL or VITE_BACKEND_HTTP_URL is provided, derive WS directly from it
  const explicitApi = import.meta.env.VITE_API_URL || import.meta.env.VITE_BACKEND_HTTP_URL;
  if (explicitApi) {
    const wsProto = (explicitApi as string).startsWith('https:') ? 'wss:' : 'ws:';
    const cleanHost = (explicitApi as string).replace(/^https?:\/\//, '').replace(/\/+$/, '');
    return `${wsProto}//${cleanHost}/ws/session`;
  }

  if (typeof window !== 'undefined') {
    const hostname = window.location.hostname || 'localhost';
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

    // Local development
    if (hostname === 'localhost' || hostname === '127.0.0.1') {
      return 'ws://localhost:8000/ws/session';
    }

    // Production builds must point at the backend explicitly via VITE_API_URL / VITE_WS_URL
    if (import.meta.env.PROD) {
      missingProdBackend();
    }

    // Local network testing
    return `${protocol}//${hostname}:8000/ws/session`;
  }

  return 'ws://localhost:8000/ws/session';
}

