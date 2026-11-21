"""Видео-клипы из кадров.
mode=api    : Veo через Gemini API или Vertex AI (модель в config.video.model)
mode=manual : промпты в tasks/flow_tasks.md, клипы делаете во Flow (руками или Claude in Chrome)
mode=still  : без нейросети, ffmpeg оживляет кадр движением камеры из motions.yaml
"""
import subprocess
import time

from . import prompts
from .config import load_yaml, secret


def _client(cfg):
    from google import genai
    v = cfg["video"]
    if v["backend"] == "vertex":
        return genai.Client(vertexai=True, project=v["project"], location=v["location"])
    return genai.Client(api_key=secret(v))


def generate_one(cfg, scn, cid, frame_path, out_path, last_frame=None):
    from google.genai import types

    v = cfg["video"]
    client = _client(cfg)
    kw = dict(aspect_ratio=scn.get("aspect", "9:16"), resolution=v["resolution"], negative_prompt=prompts.negative(scn),
              duration_seconds=v["duration"], number_of_videos=1)
    if last_frame:
        kw["last_frame"] = types.Image.from_file(location=str(last_frame))
    if v.get("generate_audio") is not None:
        kw["generate_audio"] = v["generate_audio"]
    args = dict(model=v["model"], prompt=prompts.motion_prompt(scn, cid),
                image=types.Image.from_file(location=str(frame_path)))
    try:
        op = client.models.generate_videos(**args, config=types.GenerateVideosConfig(**kw))
    except Exception as e:
        if "generate_audio" in kw or "duration" in str(e):  # не все модели принимают эти поля
            kw.pop("generate_audio", None)
            op = client.models.generate_videos(**args, config=types.GenerateVideosConfig(**kw))
        else:
            raise
    while not op.done:
        time.sleep(v["poll_seconds"])
        op = client.operations.get(op)
    if getattr(op, "error", None):
        raise RuntimeError(f"Veo ошибка: {op.error}")
    vids = (op.response.generated_videos if op.response else None) or []
    if not vids:
        raise RuntimeError("Veo не вернул видео (возможно, отказ по контент-политике)")
    video = vids[0].video
    try:
        client.files.download(file=video)
    except Exception:
        pass  # Vertex отдаёт байты сразу
    video.save(str(out_path))
    return out_path


def still_clip(frame, out, motion_key, cfg, dur=8.0):
    """Оживляет картинку: зум, панорама, тряска. Без нейросети, только ffmpeg."""
    r = cfg["render"]
    W, H, FPS = r["width"], r["height"], r["fps"]
    m = load_yaml("motions.yaml").get(motion_key or "push_in_slow", {}).get("still", {"zoom": [1.0, 1.1]})
    z0, z1 = m.get("zoom", [1.0, 1.1])
    px0, px1 = m.get("pan", [0, 0])
    py0, py1 = m.get("pan_y", [0, 0])
    sh = m.get("shake", 0)
    n = int(dur * FPS)
    if m.get("punch"):
        z = f"if(lt(on,6),{z0}-({z0}-1.0)*on/6,1.0+0.05*(on-6)/{n})"
    else:
        z = f"{z0}+({z1}-{z0})*on/{n}"
    shake = f"+if(lt(on,12),{sh}*sin(on*2.5),{sh * 0.15}*sin(on*0.7))" if sh else ""
    shy = f"+if(lt(on,12),{sh * 0.8}*cos(on*3),0)" if sh else ""
    x = f"iw/2-(iw/zoom/2)+({px0}+({px1}-{px0})*on/{n}){shake}"
    y = f"ih/2-(ih/zoom/2)+({py0}+({py1}-{py0})*on/{n}){shy}"
    vf = (f"scale={int(W * 1.3)}:{int(H * 1.3)}:force_original_aspect_ratio=increase,crop={int(W * 1.3)}:{int(H * 1.3)},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={W}x{H}:fps={FPS}")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(frame), "-vf", vf, "-t", str(dur),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p", str(out)], check=True)
    return out


def run(project, cfg, only=None, force=False, mode=None):
    scn = project.load()
    tasks = prompts.write_tasks(project, scn)
    mode = mode or cfg["video"]["mode"]
    # клипы, которые реально нужны как видео (а не как картинка с fx)
    need = {s["visual"]["clip"] for s in scn["shots"] if s.get("visual", {}).get("clip")}
    if mode == "manual":
        missing = [c for c in need if not project.find_clip(scn, c)]
        print(f"Ручной режим. Промпты и инструкции: {tasks}  (агент для Chrome: prompts/flow_agent.md)")
        print("Нет видео для:", ", ".join(sorted(missing)) if missing else "всё готово")
        return
    for cid in sorted(need):
        if only and cid not in only:
            continue
        if project.find_clip(scn, cid) and not force:
            print(f"{cid}: видео уже есть")
            continue
        frame = project.find_frame(scn, cid)
        if not frame:
            print(f"{cid}: нет кадра frames/{cid}.png, пропускаю (сначала: python -m studio frames)")
            continue
        out = project.sub("clips") / f"{cid}.mp4"
        try:
            if mode == "still":
                still_clip(frame, out, scn["clips"][cid].get("motion"),
                           {**cfg, "render": {**cfg["render"], **({"width": 1920, "height": 1080} if scn.get("aspect") == "16:9" else {})}})
            else:
                print(f"{cid}: Veo генерирует (1-3 минуты)...")
                last = scn["clips"][cid].get("last_frame")
                last = project.find_frame(scn, last) if last else None
                generate_one(cfg, scn, cid, frame, out, last)
            print(f"{cid}: ок -> {out.name}")
        except Exception as e:  # noqa
            print(f"{cid}: ОШИБКА {e}")
