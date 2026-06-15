#!/usr/bin/env python3
"""Speaker embedding for meeting voice enrollment.

Reads a JSON request on stdin:
  { "audio": "<path to webm/wav>",
    "segments": [{"speaker":"1","start_ms":0,"end_ms":4000}, ...],
    "min_dur_sec": 3 }

Writes JSON on stdout:
  { "ok": true, "embeddings": {"1": [256 floats], ...}, "durations": {"1": 21.0} }

Pipeline: ffmpeg → 16k mono wav → Kaldi-style 80-dim fbank (numpy/scipy)
          → wespeaker resnet34 ONNX → 256-d L2-normalized embedding,
          averaged per speaker over that speaker's segments.

No PyTorch — runs on onnxruntime (verified on Python 3.14).
"""
import sys, os, json, subprocess, tempfile, shutil
import numpy as np
from scipy.io import wavfile
import onnxruntime as ort

def _find_ffmpeg():
    for c in (os.environ.get("FFMPEG_PATH"), "/opt/homebrew/bin/ffmpeg",
              "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if c and os.path.exists(c):
            return c
    return shutil.which("ffmpeg") or "ffmpeg"

SR = 16000
MODEL = os.environ.get(
    "SPEAKER_EMBED_MODEL",
    os.path.expanduser("~/.claude-daemon/models/wespeaker_resnet34.onnx"),
)
MODEL_URL = ("https://huggingface.co/onnx-community/"
             "wespeaker-voxceleb-resnet34-LM/resolve/main/onnx/model.onnx")
FFMPEG = _find_ffmpeg()

def _ensure_model():
    if os.path.exists(MODEL) and os.path.getsize(MODEL) > 1_000_000:
        return
    os.makedirs(os.path.dirname(MODEL), exist_ok=True)
    import urllib.request
    tmp = MODEL + ".part"
    urllib.request.urlretrieve(MODEL_URL, tmp)
    os.replace(tmp, MODEL)

# ---- Kaldi-compatible 80-mel fbank (numpy/scipy) ----
def _mel(f): return 1127.0 * np.log(1.0 + f / 700.0)
def _imel(m): return 700.0 * (np.exp(m / 1127.0) - 1.0)

def _mel_fb(n_fft, n_mels=80, fmin=20.0, fmax=7600.0):
    fft_freqs = np.linspace(0, SR / 2, n_fft // 2 + 1)
    m_pts = np.linspace(_mel(fmin), _mel(fmax), n_mels + 2)
    f_pts = _imel(m_pts)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for i in range(n_mels):
        l, c, r = f_pts[i], f_pts[i + 1], f_pts[i + 2]
        left = (fft_freqs - l) / (c - l)
        right = (r - fft_freqs) / (r - c)
        fb[i] = np.maximum(0, np.minimum(left, right))
    return fb

_MEL_FB = _mel_fb(512)

def _fbank(sig):
    sig = sig.astype(np.float32)
    sig = np.append(sig[0], sig[1:] - 0.97 * sig[:-1])  # pre-emphasis
    flen, fshift = 400, 160  # 25ms / 10ms @16k
    if len(sig) < flen:
        return None
    n = 1 + (len(sig) - flen) // fshift
    idx = np.arange(flen)[None, :] + fshift * np.arange(n)[:, None]
    frames = sig[idx]
    win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(flen) / (flen - 1))) ** 0.85
    frames = frames * win
    spec = np.abs(np.fft.rfft(frames, 512)) ** 2
    feats = np.log(np.maximum(spec @ _MEL_FB.T, 1e-10))
    feats = feats - feats.mean(axis=0, keepdims=True)  # CMN
    return feats.astype(np.float32)

def _to_wav(audio_path):
    if audio_path.lower().endswith(".wav"):
        return audio_path, False
    tmp = tempfile.mktemp(suffix=".wav")
    subprocess.run([FFMPEG, "-y", "-i", audio_path, "-ar", str(SR), "-ac", "1", tmp],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return tmp, True

def main():
    req = json.load(sys.stdin)
    audio_path = req["audio"]
    segments = req.get("segments", [])
    min_dur = float(req.get("min_dur_sec", 3))

    _ensure_model()
    wav_path, cleanup = _to_wav(audio_path)
    try:
        sr, audio = wavfile.read(wav_path)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        audio = audio.astype(np.float32)
        if np.abs(audio).max() > 1:
            audio = audio / 32768.0
    finally:
        if cleanup:
            try: os.unlink(wav_path)
            except OSError: pass

    sess = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])

    # gather audio per speaker
    by_speaker = {}
    dur = {}
    for s in segments:
        sp = str(s["speaker"])
        a = int(s.get("start_ms", 0) / 1000 * SR)
        b = int(s.get("end_ms", 0) / 1000 * SR)
        b = min(b, len(audio))
        if b - a < int(0.5 * SR):
            continue
        by_speaker.setdefault(sp, []).append(audio[a:b])
        dur[sp] = dur.get(sp, 0.0) + (b - a) / SR

    embeddings = {}
    for sp, chunks in by_speaker.items():
        if dur.get(sp, 0) < min_dur:
            continue
        # embed each chunk, average, renormalize
        vecs = []
        for ch in chunks:
            feats = _fbank(ch)
            if feats is None:
                continue
            out = sess.run(None, {"input_features": feats[None]})[0][0]
            vecs.append(out / (np.linalg.norm(out) + 1e-9))
        if not vecs:
            continue
        mean = np.mean(vecs, axis=0)
        mean = mean / (np.linalg.norm(mean) + 1e-9)
        embeddings[sp] = [float(x) for x in mean]

    json.dump({"ok": True, "embeddings": embeddings,
               "durations": {k: round(v, 1) for k, v in dur.items()}}, sys.stdout)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        json.dump({"ok": False, "error": str(e)}, sys.stdout)
        sys.exit(1)
