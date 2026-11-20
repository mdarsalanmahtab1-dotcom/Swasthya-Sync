/**
 * Pre-recorded audio for the kiosk's fixed guide prompts (public/audio/guide/, built by
 * backend/scripts/build_guide_audio.py). A file is used only when its recorded text matches
 * the prompt exactly, so editing a prompt never plays stale audio — it falls back to live TTS.
 */
type Manifest = Record<string, Record<string, string>>;

let manifestPromise: Promise<Manifest> | null = null;

function loadManifest(): Promise<Manifest> {
  if (!manifestPromise) {
    manifestPromise = fetch('/audio/guide/manifest.json')
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}));
  }
  return manifestPromise;
}

// Warm the manifest early so the first prompt does not wait for it.
if (typeof window !== 'undefined') void loadManifest();

export async function guideAudioUrl(lang: string, key: string, text: string): Promise<string | null> {
  const manifest = await loadManifest();
  return manifest?.[lang]?.[key] === text ? `/audio/guide/${lang}/${key}.wav` : null;
}
