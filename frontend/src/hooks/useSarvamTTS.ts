import { useState, useRef, useCallback, useEffect } from 'react';
import { getApiBaseUrl } from '../config';
import { useAudioGuideContext } from '../context/AudioGuideContext';

interface UseSarvamTTSReturn {
  speak: (text: string, language: string, loopCount?: number, purpose?: 'interview' | 'guide', staticUrl?: string) => Promise<void>;
  stop: () => void;
  isSpeaking: boolean;
  error: string | null;
}

const BACKEND_URL = getApiBaseUrl();

// Browser voices spell Latin words inside Indic sentences letter by letter ("S W A S T H Y A").
// Mirror the backend's pronounceable() for the fallback voice.
const BRAND_SPOKEN: Record<string, string> = {
  hi: 'स्वास्थ्य सिंक', mr: 'स्वास्थ्य सिंक', bn: 'স্বাস্থ্য সিঙ্ক', gu: 'સ્વાસ્થ્ય સિંક',
  pa: 'ਸਵਾਸਥ ਸਿੰਕ', or: 'ସ୍ୱାସ୍ଥ୍ୟ ସିଙ୍କ', ta: 'ஸ்வாஸ்த்ய சிங்க்', te: 'స్వాస్థ్య సింక్',
  kn: 'ಸ್ವಾಸ್ಥ್ಯ ಸಿಂಕ್', ml: 'സ്വാസ്ഥ്യ സിങ്ക്',
};
const ACRONYMS: Record<string, Record<string, string>> = {
  hi: { ABHA: 'आभा', OTP: 'ओटीपी', ID: 'आईडी', BP: 'बीपी', PDF: 'पीडीएफ़' },
  mr: { ABHA: 'आभा', OTP: 'ओटीपी', ID: 'आयडी', BP: 'बीपी' },
  bn: { ABHA: 'আভা', OTP: 'ওটিপি', ID: 'আইডি', BP: 'বিপি', PDF: 'পিডিএফ' },
};
export function speakableText(text: string, language: string): string {
  const lang = (language || 'en-IN').slice(0, 2).toLowerCase();
  let out = (text || '').replace(/swasth?ya[\s-]?sync/gi, BRAND_SPOKEN[lang] || 'Swasthya Sync');
  const map = ACRONYMS[lang];
  if (map) out = out.replace(/\b(ABHA|OTP|ID|BP|PDF)\b/g, (m) => map[m] || m);
  return out;
}

export function useSarvamTTS(): UseSarvamTTSReturn {
  const { isMuted, registerAudioElement, registerAbortController } = useAudioGuideContext();
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const blobUrlRef = useRef<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const loopTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stop = useCallback(() => {
    if (loopTimeoutRef.current) {
      clearTimeout(loopTimeoutRef.current);
      loopTimeoutRef.current = null;
    }
    if (abortControllerRef.current) {
      try {
        abortControllerRef.current.abort();
      } catch {}
      abortControllerRef.current = null;
    }
    registerAbortController(null);

    if (audioRef.current) {
      audioRef.current.onplay = null;
      audioRef.current.onended = null;
      audioRef.current.onerror = null;
      try {
        audioRef.current.pause();
        audioRef.current.src = '';
      } catch {}
      audioRef.current = null;
    }
    registerAudioElement(null);

    if (blobUrlRef.current) {
      try {
        URL.revokeObjectURL(blobUrlRef.current);
      } catch {}
      blobUrlRef.current = null;
    }

    // Cancel native browser SpeechSynthesis
    if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
      try {
        window.speechSynthesis.cancel();
      } catch {}
    }

    setIsSpeaking(false);
  }, [registerAudioElement, registerAbortController]);

  useEffect(() => {
    return () => {
      stop();
    };
  }, [stop]);

  // If kiosk is muted, immediately stop everything
  useEffect(() => {
    if (isMuted) {
      stop();
    }
  }, [isMuted, stop]);

  const speak = useCallback(async (text: string, language: string, loopCount: number = 1, purpose: 'interview' | 'guide' = 'guide', staticUrl?: string) => {
    if (!text?.trim() || isMuted) return;

    stop(); // Stop any currently playing audio and abort in-flight requests
    setError(null);

    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    registerAbortController(abortController);

    const playFallback = () => {
      if (typeof window !== 'undefined' && 'speechSynthesis' in window && !isMuted) {
        window.speechSynthesis.cancel();
        const targetLang = language || 'hi-IN';
        const utterance = new SpeechSynthesisUtterance(speakableText(text, targetLang));
        utterance.lang = targetLang;

        const voices = window.speechSynthesis.getVoices();
        const langPrefix = targetLang.slice(0, 2).toLowerCase();
        const matchingVoice = voices.find(v => {
          const vLang = v.lang.toLowerCase().replace('_', '-');
          return vLang.startsWith(langPrefix) ||
            (langPrefix === 'bn' && (v.name.toLowerCase().includes('bengali') || v.name.toLowerCase().includes('bangla')));
        });
        if (matchingVoice) {
          utterance.voice = matchingVoice;
        } else if (langPrefix !== 'en' && voices.length > 0) {
          // No voice for this language on this device: an English voice would read the
          // script letter by letter. Stay silent; the text is on screen.
          console.warn(`[TTS] No ${targetLang} browser voice; skipping fallback speech`);
          return true;
        }
        console.warn('[TTS] Gemini voice unavailable; using the browser voice');

        utterance.onstart = () => setIsSpeaking(true);
        
        let fallbackLoops = loopCount;
        utterance.onend = () => {
          if (fallbackLoops > 1 && !isMuted) {
            fallbackLoops--;
            loopTimeoutRef.current = setTimeout(() => {
              if (abortControllerRef.current === abortController && !abortController.signal.aborted && !isMuted) {
                window.speechSynthesis.speak(utterance);
              }
            }, 1500); // 1.5s gap
          } else {
            setIsSpeaking(false);
          }
        };
        utterance.onerror = () => setIsSpeaking(false);
        window.speechSynthesis.speak(utterance);
        return true;
      }
      return false;
    };

    try {
      const formData = new FormData();
      formData.append('text', text);
      formData.append('language', language || 'hi-IN');
      formData.append('purpose', purpose);

      // Pre-recorded file first (fixed guide prompts), then live TTS.
      let response: Response | null = null;
      if (staticUrl) {
        try {
          const r = await fetch(staticUrl, { signal: abortController.signal });
          if (r.ok) response = r;
        } catch (e: any) {
          if (e?.name === 'AbortError') throw e;
        }
      }
      if (!response) {
        response = await fetch(`${BACKEND_URL}/api/tts`, {
          method: 'POST',
          body: formData,
          signal: abortController.signal,
        });
      }

      if (!response.ok) {
        if (playFallback()) return;
        throw new Error(`TTS failed: ${response.status}`);
      }

      const audioBlob = await response.blob();
      
      // If we got aborted while waiting for response or muted, cancel
      if (abortController.signal.aborted || isMuted) return;
      
      const blobUrl = URL.createObjectURL(audioBlob);
      blobUrlRef.current = blobUrl;

      const audio = new Audio(blobUrl);
      audioRef.current = audio;
      registerAudioElement(audio);

      let loopsRemaining = loopCount;

      audio.onplay = () => setIsSpeaking(true);
      audio.onended = () => {
        if (loopsRemaining > 1 && !isMuted) {
          loopsRemaining--;
          loopTimeoutRef.current = setTimeout(() => {
            if (audioRef.current === audio && abortControllerRef.current === abortController && !abortController.signal.aborted && !isMuted) {
              audio.play().catch(console.error);
            }
          }, 1500); // 1.5s gap between loops
        } else {
          setIsSpeaking(false);
          URL.revokeObjectURL(blobUrl);
          if (blobUrlRef.current === blobUrl) {
            blobUrlRef.current = null;
          }
        }
      };
      audio.onerror = () => {
        setIsSpeaking(false);
        setError('Audio playback failed');
      };

      if (!isMuted && !abortController.signal.aborted) {
        await audio.play();
      }
    } catch (err: any) {
      if (err?.name === 'AbortError') {
        return;
      }
      try {
        if (playFallback()) return;
      } catch (e) {
        console.error('WebSpeech fallback failed', e);
      }
      const msg = err instanceof Error ? err.message : 'TTS request failed';
      setError(msg);
      setIsSpeaking(false);
    }
  }, [stop, isMuted, registerAbortController, registerAudioElement]);

  return { speak, stop, isSpeaking, error };
}
