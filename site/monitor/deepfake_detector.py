"""Evidence from the actual media. A model signal alone never confirms a deepfake."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

REFERENCE = {
    "title": "Видео от имени Абдуллы Арипова",
    "publication": "2024-01-15",
    "article": "https://www.factchecker.uz/post/o-zbekiston-bosh-vaziri-abdulla-aripov-bayonoti",
    "video": "https://www.youtube.com/watch?v=TYvW1LVJ7VM",
    "sha256": "ac0d82a0fdef943da770d3c3d27a8fd7206e209b2f82f104583cb52f9878359e",
    "video_hash": "82e520e5c57821d6458a43d297a30fd7708eab0aed0ad6fef19128cd56f6a58d",
    "audio_hash": "405c3f545920686a743575ed161e0e57363b880259d04aae1fa4ea62b060f3cc",
}
MODELS = {"face": "/models/face-vit-q8.onnx", "voice": "/models/voice-wav2vec.onnx"}

def run(args, timeout=25):
    return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, check=True, timeout=timeout).stdout

def fingerprint(path):
    with open(path, "rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    streams = {}
    try:
        raw = run(["ffmpeg", "-v", "error", "-protocol_whitelist", "file,pipe","-format_whitelist","mov,matroska,webm", "-i", str(path),
                   "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-"])
        for line in raw.decode().splitlines():
            _, kind, value = line.split(",")
            streams[kind] = value.partition("=")[2].strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return digest, streams

def reference_match(digest, streams):
    return (digest == REFERENCE["sha256"] or
            (streams.get("v") == REFERENCE["video_hash"] and streams.get("a") == REFERENCE["audio_hash"]))

def assess(signals, matched):
    if matched:
        return "confirmed", "Запись совпала с образцом из разбора Factchecker.uz. Редакция установила подмену голоса и обработку изображения."
    if any(sum(p["score"] >= .95 for p in s.get("samples", [])) >= 2 for s in signals):
        return "review", "Детектор дал высокий сигнал на нескольких фрагментах. Для подтверждения нужна сверка с оригиналом."
    if any(s.get("samples") for s in signals):
        return "unknown", "Проверены доступные фрагменты. Доказательств для подтверждения подмены недостаточно."
    return "unknown", "Не удалось получить пригодные для анализа фрагменты лица или речи."

def extract(path, point, args):
    return run(["ffmpeg", "-v", "error", "-threads", "1", "-protocol_whitelist", "file,pipe","-format_whitelist","mov,matroska,webm", "-ss", str(point),
                "-i", str(path), "-threads", "1", *args, "-"])

def moments(duration, window, count):
    last = duration - window
    if last < 0:
        return []
    n = min(count, max(1, int(duration // window)))
    return [round((i + .5) * last / n, 2) for i in range(n)]

def model_samples(path, duration, kind):
    import numpy as np
    import onnxruntime as ort
    if kind == "face":
        import cv2
        cv2.setNumThreads(1)
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    else:
        import webrtcvad
        vad = webrtcvad.Vad(2)
    opt = ort.SessionOptions()
    opt.intra_op_num_threads = 2
    opt.inter_op_num_threads = 1
    opt.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    session = None
    points = moments(duration, 4 if kind == "voice" else 1, 5 if kind == "voice" else 8)
    samples = []
    for t in points:
        try:
            if kind == "face":
                raw = extract(path, t, ["-frames:v", "1", "-vf", "scale=640:-2", "-vcodec", "mjpeg", "-f", "image2pipe"])
                if not raw:
                    continue
                img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    continue
                faces = cascade.detectMultiScale(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 1.12, 5, minSize=(42,42))
                if len(faces) == 0:
                    continue
                x, y, w, h = max(faces, key=lambda f: int(f[2]) * int(f[3]))
                pad = int(w * .20)
                crop = img[max(0,y-pad):y+h+pad, max(0,x-pad):x+w+pad]
                rgb = cv2.resize(crop, (224,224))[:,:,::-1].astype("float32") / 127.5 - 1
                value = rgb.transpose(2,0,1)[None].copy()
            else:
                raw = extract(path, t, ["-t", "4", "-vn", "-ar", "16000", "-ac", "1", "-f", "s16le"])
                chunks = [raw[j:j+960] for j in range(0, len(raw)-959, 960)]
                if len(chunks) < 110 or sum(vad.is_speech(c,16000) for c in chunks) < len(chunks)*.6:
                    continue
                wav = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768
                if wav.std() < .003:
                    continue
                value = ((wav - wav.mean()) / np.sqrt(wav.var() + 1e-7))[None]
        except subprocess.SubprocessError:
            continue
        if session is None:
            session = ort.InferenceSession(MODELS[kind], sess_options=opt, providers=["CPUExecutionProvider"])
        logits = session.run(None, {session.get_inputs()[0].name: value})[0].reshape(-1)
        if len(logits) != 2 or not np.isfinite(logits).all():
            raise ValueError("invalid logits")
        exp = np.exp(logits - logits.max())
        score = float(exp[1 if kind == "face" else 0] / exp.sum())
        samples.append({"time": t, "score": round(score, 6)})
    return {"kind": kind, "status": "checked" if samples else "no_samples", "samples": samples, "points": len(points)}

def perceptual_signature(path, duration):
    """Aligned samples for screening complete re-encodes of the reference clip."""
    import numpy as np
    frames, audio = [], []
    # Stay clear of first/last title cards. Compare audio energy changes within bands.
    for frac in (.11,.26,.41,.56,.71,.86):
        t = round(duration*frac,2)
        frame = extract(path,t,["-frames:v","1","-vf","scale=17:16,format=gray","-f","rawvideo"])
        raw = extract(path,t,["-t","1.5","-vn","-ar","8000","-ac","1","-f","s16le"])
        if len(frame)!=272 or len(raw)<22400:
            return None
        img = np.frombuffer(frame,np.uint8).reshape(16,17)
        if img.std()<8:
            return None
        frames.append(np.packbits(img[:,1:]>img[:,:-1]).tobytes().hex())
        wave = np.frombuffer(raw,"<i2").astype("float32")[:11200].reshape(35,320)
        energy = np.abs(np.fft.rfft(wave*np.hanning(320),axis=1))[:,2:146].reshape(35,12,12).mean(axis=2)
        change = np.diff(np.log1p(energy),axis=0)
        if wave.std()<80 or abs(change).mean()<.04:
            return None
        audio.append(np.packbits(change>0).tobytes().hex())
    return {"frames":frames,"audio":audio}

def compare_signatures(a,b):
    if not a or not b:
        return False
    for key,limit in (("frames",.21),("audio",.19)):
        if len(a[key])!=6 or len(b[key])!=6:
            return False
        for left,right in zip(a[key],b[key]):
            if len(left)!=len(right) or not left:
                return False
            errors=(int(left,16)^int(right,16)).bit_count()
            if errors/(4*len(left))>limit:
                return False
    return True

def similar_reference(path,duration):
    reference_file=Path(__file__).with_name("reference-signature.json")
    if abs(duration-36.08381)>.6 or not reference_file.is_file():
        return False
    try:
        return compare_signatures(perceptual_signature(path,duration),json.loads(reference_file.read_bytes()))
    except (ImportError,OSError,ValueError,KeyError,subprocess.SubprocessError):
        return False

def inspect(path):
    digest, streams = fingerprint(path)
    raw = run(["ffprobe","-v","error","-protocol_whitelist","file,pipe","-format_whitelist","mov,matroska,webm","-show_entries","format=duration:stream=codec_type","-of","json",str(path)])
    meta = json.loads(raw)
    types = [s.get("codec_type") for s in meta.get("streams",[])]
    if types.count("video") != 1 or types.count("audio") != 1:
        streams = {}
    matched = reference_match(digest, streams)
    duration = float(meta["format"]["duration"])
    if not 0 < duration <= 601:
        raise ValueError("Запись вне лимита 10 минут.")
    signals = []
    for kind in MODELS:
        try:
            out = run([sys.executable, __file__, kind, str(path), str(duration)], timeout=110)
            signals.append(json.loads(out))
        except (OSError, ValueError, subprocess.SubprocessError):
            signals.append({"kind":kind, "status":"unavailable", "samples":[]})
    state, reason = assess(signals, matched)
    similar = not matched and similar_reference(path,duration)
    if similar:
        state, reason = "review", "Звук и изображение в шести точках похожи на архивный дипфейк после перекодирования. Требуется проверка копии."
    return {"state":state, "reason":reason, "signals":signals, "similar_reference":similar, "sha256":digest, "duration":round(duration,2),
            "reference":REFERENCE["article"] if matched or similar else "", "exact_match":matched}

if __name__ == "__main__":
    print(json.dumps(model_samples(Path(sys.argv[2]), float(sys.argv[3]), sys.argv[1])))
