/**
 * SwasthyaSync — WebSocket Conversation Hook
 *
 * Manages the WebSocket lifecycle to the backend Dialogue Manager.
 * The server pushes UI instructions, the client renders them.
 * The client sends patient responses (tap selections or ASR transcripts).
 */

import { useState, useEffect, useRef, useCallback } from 'react';
// Routing is now handled in App.tsx
import type { OrbState } from '../components/AbstractOrb';
import { getWsUrl } from '../config';
import { livePlayer } from '../lib/liveAudio';

/** Gemini Live voice state: off → starting → active, or fallback (Gemini STT/TTS). */
export type LiveState = 'off' | 'starting' | 'active' | 'fallback';

// Types matching the backend's UI instruction format
export interface UIOption {
  label: string;          // Displayed text (may be translated)
  value?: string;         // Backend value (always English, for slot matching)
  label_translated?: string; // Alternate translated label (for chief complaint screen)
  icon: string | null;
}

export interface RedFlag {
  rule_id: string;
  description: string;
}

export interface ProgressInfo {
  done: number;
  total: number;
}

export interface UIInstruction {
  type?: string;
  macro_state: string;
  clinic_mode: string;
  session_id: string;
  language: string;
  screen: string;
  orb_state: OrbState;

  // Conversation screen fields
  prompt?: string;
  ack?: string | null;
  prompt_subtitle?: string;
  options?: UIOption[];
  current_slot_id?: string;
  current_field_id?: string;
  section_label?: string;
  template_name?: string;
  progress?: ProgressInfo;
  can_skip?: boolean;
  section_summary?: string;
  conversation_history?: any[];

  // Red-flag fields
  red_flags?: RedFlag[];

  // Summary/Complete fields
  patient_record?: any;
  patient_name?: string;

  // Gemini Live: 'live' means the prompt was (or is being) spoken by Live — don't TTS it
  voice?: string;
}

interface UseConversationReturn {
  ui: UIInstruction | null;
  orbState: OrbState;
  isConnected: boolean;
  isProcessing: boolean;
  startSession: (clinicMode?: string, language?: string, demographics?: { name?: string; age?: number | null; sex?: string; weight?: number | null; height?: string | null; vitals?: string | null }, patientId?: string, sessionId?: string, previousHistory?: any) => void;
  resumeSession: (sessionId: string) => void;
  sendInput: (inputType: string, value: string) => void;
  sendRedflag: () => void;
  clearRedflag: () => void;
  getRecord: () => void;
  liveState: LiveState;
  startLive: (resume?: boolean) => void;
  sendRaw: (data: object) => void;
}

export function useConversation(): UseConversationReturn {

  const [ui, setUi] = useState<UIInstruction | null>(null);
  const [orbState, setOrbState] = useState<OrbState>('idle');
  const [isConnected, setIsConnected] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [liveState, setLiveState] = useState<LiveState>('off');
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<number | null>(null);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    let targetWsUrl = getWsUrl();
    const storedSession = localStorage.getItem('kiosk_session_id');
    if (storedSession) {
      targetWsUrl += `?session_id=${storedSession}`;
    }
    console.log('[WS] Connecting to:', targetWsUrl);
    const ws = new WebSocket(targetWsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      console.log('[WS] Connected to SwasthyaSync backend');
      setIsConnected(true);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);

        // ── Gemini Live voice stream ──
        if (msg.type === 'audio_out') {
          livePlayer.play(msg.data, msg.rate || 24000);
          return;
        }
        if (msg.type === 'audio_out_stop') {
          livePlayer.stop();
          return;
        }
        if (msg.type === 'audio_out_end') {
          return;
        }
        if (msg.type === 'live_status') {
          setLiveState(msg.state === 'active' ? 'active' : 'fallback');
          if (msg.state !== 'active') livePlayer.stop();
          return;
        }
        if (msg.type === 'live_notice') {
          setIsProcessing(false);
          setOrbState('idle');
          return;
        }

        if (msg.type === 'orb_state') {
          setOrbState(msg.orb_state as OrbState);
          setIsProcessing(true);
          return;
        }

        if (msg.type === 'ui') {
          const uiMsg = msg as UIInstruction;
          setUi(uiMsg);
          setOrbState((msg.orb_state as OrbState) || 'idle');
          setIsProcessing(false);
          
          if (uiMsg.session_id) {
            localStorage.setItem('kiosk_session_id', uiMsg.session_id);
          }

          if (uiMsg.screen === 'complete') {
            localStorage.removeItem('kiosk_session_id');
          }
          
          // Routing is handled in App.tsx RouteSynchronizer
          
          return;
        }

        if (msg.type === 'record') {
          console.log('[WS] Patient record:', msg);
          return;
        }

        if (msg.type === 'error') {
          console.error('[WS] Server error:', msg.message);
          setIsProcessing(false);
          if (msg.message === 'Session not found or expired') {
            localStorage.removeItem('kiosk_session_id');
            // Mid-interview (after a server restart): go back to the start instead of hanging
            const path = window.location.pathname;
            if (path.startsWith('/kiosk/') && !path.startsWith('/kiosk/login')) {
              window.location.assign('/kiosk/login');
            }
          }
          return;
        }
      } catch (e) {
        console.error('[WS] Parse error:', e);
      }
    };

    ws.onclose = () => {
      console.log('[WS] Disconnected');
      setIsConnected(false);
      setLiveState('off'); // the server closes the Live session with the socket
      livePlayer.stop();
      // Auto-reconnect after 2s
      reconnectTimer.current = window.setTimeout(connect, 2000);
    };

    ws.onerror = (err) => {
      console.error('[WS] Error:', err);
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const send = useCallback((data: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    } else {
      console.warn('[WS] Not connected — cannot send');
    }
  }, []);

  const startSession = useCallback((
    clinicMode = 'allopathic', 
    language = 'en-IN', 
    demographics?: { name?: string; age?: number | null; sex?: string; weight?: number | null; height?: string | null; vitals?: string | null },
    patientId?: string,
    sessionId?: string,
    previousHistory?: any
  ) => {
    setLiveState('off');
    send({
      type: 'start',
      clinic_mode: clinicMode,
      language,
      patient_name: demographics?.name || '',
      patient_age: demographics?.age || null,
      patient_sex: demographics?.sex || '',
      patient_weight: demographics?.weight || null,
      patient_height: demographics?.height || null,
      patient_vitals: demographics?.vitals || null,
      patient_id: patientId,
      session_id: sessionId,
      previous_history: previousHistory
    });
  }, [send]);

  const resumeSession = useCallback((sessionId: string) => {
    send({
      type: 'resume',
      session_id: sessionId
    });
  }, [send]);

  const sendInput = useCallback((inputType: string, value: string) => {
    setIsProcessing(true);
    setOrbState('processing');
    send({ type: 'input', input_type: inputType, value });
  }, [send]);

  const sendRedflag = useCallback(() => {
    if (ui) {
      send({ type: 'redflag' });
    }
  }, [send, ui]);

  const clearRedflag = useCallback(() => {
    send({ type: 'clear_redflag' });
  }, [send]);

  const getRecord = useCallback(() => {
    send({ type: 'get_record' });
  }, [send]);

  const startLive = useCallback((resume = false) => {
    setLiveState('starting');
    send({ type: 'live_start', resume });
  }, [send]);

  return {
    ui,
    orbState,
    isConnected,
    isProcessing,
    startSession,
    resumeSession,
    sendInput,
    sendRedflag,
    clearRedflag,
    getRecord,
    liveState,
    startLive,
    sendRaw: send,
  };
}
