"""Сборка ролика: шоты режутся ровно по началу реплик, субтитры, бренд, плашка источника,
голоса, музыка с ducking под голос, звуки на склейках, громкость -14 LUFS."""
import json
import subprocess

from . import align, captions, music, sfx
from .config import load_yaml
from .videogen import still_clip

SFX_DEFAULTS = {
    "boom": {"vol": 0.9, "dur": 2.5}, "fireworks_show": {"vol": 0.45, "dur": None, "lead": -0.2, "fade_in": 0.4},
    "ding": {"vol": 0.5, "dur": 1.2}, "buzz": {"vol": 0.45, "dur": 0.6}, "stamp": {"vol": 0.6, "dur": 0.6},
    "pop": {"vol": 0.4, "dur": 0.2}, "tick": {"vol": 0.5, "dur": 2.0}, "cash": {"vol": 0.45, "dur": 0.9},
    "sparkle": {"vol": 0.4, "dur": 1.5}, "whoosh": {"vol": 0.28, "dur": 0.8},
}


def run_ff(args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def probe_dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def timeline(scn, manifest, R):
    tm = {**{k: R[k] for k in ("lead", "gap", "outro")}, **scn.get("timing", {})}
    t = tm["lead"]
    rows = []
    for s in scn["shots"]:
        if s["id"] not in manifest:
            raise RuntimeError(f"Нет озвучки для {s['id']}. Сначала: python -m studio voice <проект>")
        d = manifest[s["id"]]["dur"]
        rows.append({"id": s["id"], "start": round(t, 3), "dur": d})
        t = t + d + tm["gap"] + s.get("hold", 0)
    starts = [0.0] + [r["start"] for r in rows[1:]]
    T = rows[-1]["start"] + rows[-1]["dur"] + tm["outro"]
    ends = starts[1:] + [T]
    for r, a, b in zip(rows, starts, ends):
        r["v0"], r["v1"] = round(a, 3), round(b, 3)
    return rows, T


def _shot_video(project, scn, s, d, out, cfg, R):
    W, H, FPS = R["width"], R["height"], R["fps"]
    v = s.get("visual", {})
    eq = f"eq=saturation={R['saturation']}:contrast={R['contrast']}"
    enc = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p", str(out)]
    if v.get("image"):
        img = project.find_frame(scn, v["image"])
        if not img:
            return _placeholder(out, d, f"нет кадра {v['image']}", R)
        n = int(d * FPS)
        if v.get("fx", "slam") == "slam":
            vf = (f"scale={int(W * 1.25)}:{int(H * 1.25)}:force_original_aspect_ratio=increase,crop={int(W * 1.25)}:{int(H * 1.25)},"
                  f"zoompan=z='if(lt(on,6),1.25-0.04*on,1.0+0.05*(on-6)/{n})':"
                  f"x='iw/2-(iw/zoom/2)+if(lt(on,10),18*sin(on*2.5),0)':y='ih/2-(ih/zoom/2)+if(lt(on,10),14*cos(on*3),0)'"
                  f":d={n}:s={W}x{H}:fps={FPS},{eq}")
        else:  # плавный наезд
            vf = (f"scale={int(W * 1.2)}:{int(H * 1.2)}:force_original_aspect_ratio=increase,crop={int(W * 1.2)}:{int(H * 1.2)},"
                  f"zoompan=z='1.0+0.1*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={n}:s={W}x{H}:fps={FPS},{eq}")
        run_ff(["-loop", "1", "-i", str(img), "-vf", vf, "-t", f"{d:.3f}", *enc])
        return out
    if v.get("file"):
        src = project.dir / v["file"]
    else:
        cid = v.get("clip")
        src = project.find_clip(scn, cid) if cid else None
        if not src and cid:
            fr = project.find_frame(scn, cid)
            if fr:  # нет видео, но есть кадр: оживляем ffmpeg-ом
                src = project.sub("work") / f"still_{cid}.mp4"
                if not src.exists():
                    still_clip(fr, src, scn["clips"].get(cid, {}).get("motion"), {**cfg, "render": R})
        if not src:
            return _placeholder(out, d, f"нет клипа {cid}", R)
    off = float(v.get("offset", 0))
    clen = probe_dur(src)
    need = off + d
    slow = max(1.0, need / (clen - 0.1)) if need > clen - 0.1 else 1.0
    slow = min(slow, 1.6)  # сильнее не замедляем, дальше держим последний кадр
    zoom = (f"zoompan=z='1.0+{R['push_in']}*on/{max(1, int(d * FPS))}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={W}x{H}:fps={FPS}")
    vf = (f"trim=start={off / slow:.3f},setpts=PTS-STARTPTS,setpts={slow:.4f}*PTS,"
          f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},fps={FPS},"
          f"tpad=stop_mode=clone:stop_duration={d + 1:.2f},{zoom},trim=duration={d:.3f},setpts=PTS-STARTPTS,{eq}")
    run_ff(["-i", str(src), "-vf", vf, *enc])
    return out


def _placeholder(out, d, label, R):
    if not R.get("draft"):
        raise SystemExit(f"Сборка остановлена: {label}. Сделайте кадры и видео по tasks/flow_playbook.md "
                         f"(python -m studio prompts <проект>) или соберите черновик: build --draft")
    print(f"  ! {label}: ставлю заглушку (черновик)")
    run_ff(["-f", "lavfi", "-i", f"color=c=0x1d2340:s={R['width']}x{R['height']}:r={R['fps']}:d={d:.3f}",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)])
    return out


def build(project, cfg, out_name=None, reuse_video=False):
    scn = project.load()
    R = {**cfg["render"], **scn.get("render", {})}
    if scn.get("aspect") == "16:9":
        R["width"], R["height"] = 1920, 1080
    W, H = R["width"], R["height"]
    work = project.sub("work")
    manifest = json.loads((project.sub("voice") / "manifest.json").read_text(encoding="utf-8"))
    rows, T = timeline(scn, manifest, R)
    aud = load_yaml("audiences.yaml").get(scn.get("audience", "teens"), {})
    lang = scn.get("lang", "ru")

    # 1. субтитры: тайминг по словам
    caps = []
    for s, r in zip(scn["shots"], rows):
        chunks = s.get("captions") or align.auto_chunks(s["text"])
        need_whisper = (not all(len(c) >= 4 for c in chunks) and len(chunks) > 1
                        and manifest[s["id"]].get("src") != "mock")
        wl = (align.words(project.sub("voice") / f"{s['id']}.wav", lang, cfg, work, s["text"])
              if need_whisper else [])
        for (txt, col, a, b) in align.chunk_times(chunks, wl, r["dur"], s["text"]):
            caps.append((txt, col, r["start"] + a, r["start"] + b))
    cscale = aud.get("caption_scale", 1.0)
    for k, (txt, col, a, b) in enumerate(caps):
        captions.caption(work / f"cap{k}a.png", txt, col, W, H, 0.82, cscale)
        captions.caption(work / f"cap{k}b.png", txt, col, W, H, 1.0, cscale)
    captions.brand(work / "brand.png", W, H, scn.get("brand", R["brand"]), R["brand_color"])
    plate_lines = scn.get("source", {}).get("plate") or ["ИСТОЧНИК: LEX.UZ"]
    captions.plate(work / "plate.png", plate_lines, W, H, R["brand_color"])

    vid = work / "vid.mp4"
    if not (reuse_video and vid.exists()):
        # 2. шоты
        parts = []
        for i, (s, r) in enumerate(zip(scn["shots"], rows)):
            d = round(r["v1"] - r["v0"], 3)
            print(f"  шот {s['id']}: {r['v0']:6.2f}-{r['v1']:6.2f}  {s.get('visual')}")
            parts.append(_shot_video(project, scn, s, d, work / f"shot{i:02d}.mp4", cfg, R))
        (work / "list.txt").write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        run_ff(["-f", "concat", "-safe", "0", "-i", str(work / "list.txt"), "-c", "copy", str(work / "base.mp4")])
        # 3. слои
        inputs = ["-i", str(work / "base.mp4"), "-i", str(work / "brand.png"), "-i", str(work / "plate.png")]
        fc = "[0:v][1:v]overlay=0:0[v0];"
        cur, idx = "v0", 3
        for k, (_, _, a, b) in enumerate(caps):
            inputs += ["-i", str(work / f"cap{k}a.png"), "-i", str(work / f"cap{k}b.png")]
            fc += (f"[{cur}][{idx}:v]overlay=0:0:enable='between(t,{a:.3f},{a + 0.07:.3f})'[x{k}];"
                   f"[x{k}][{idx + 1}:v]overlay=0:0:enable='between(t,{a + 0.07:.3f},{b:.3f})'[y{k}];")
            cur, idx = f"y{k}", idx + 2
        pl = scn.get("plate_at")
        if pl:
            pl_t = next(r["start"] for r in rows if r["id"] == pl)
        else:
            pl_t = rows[-1]["start"] + rows[-1]["dur"] - 0.3
        fc += f"[{cur}][2:v]overlay=0:0:enable='gte(t,{pl_t:.2f})',fade=t=out:st={T - 0.35:.2f}:d=0.35[vout]"
        run_ff([*inputs, "-filter_complex", fc, "-map", "[vout]", "-c:v", "libx264", "-preset", R["preset"],
                "-crf", str(R["crf"]), "-pix_fmt", "yuv420p", str(vid)])

    # 4. звук
    ai, af = [], ""

    def addin(f):
        ai.extend(["-i", str(f)])
        return len(ai) // 2  # 0 = видео

    vl = []
    for i, (s, r) in enumerate(zip(scn["shots"], rows)):
        src = addin(project.sub("voice") / f"{s['id']}.wav")
        vol = s.get("voice_volume", 1.0 if s.get("speaker", "narrator") == "narrator" else 1.1)
        ms = int(r["start"] * 1000)
        af += f"[{src}:a]volume={vol},adelay={ms}|{ms}[vo{i}];"
        vl.append(f"[vo{i}]")
    af += "".join(vl) + f"amix=inputs={len(vl)}:normalize=0,aresample=44100,pan=stereo|c0=c0|c1=c0[voice];"
    af += "[voice]asplit=2[vmix][vsc];"
    mus = scn.get("music", {})
    if mus.get("file"):
        mfile = project.dir / mus["file"]
    else:
        mfile = music.synth(work / "music.wav", T + 1, mus.get("preset") or aud.get("music", "upbeat"))
    m = addin(mfile)
    mv = mus.get("volume", R["music_volume"])
    af += (f"[{m}:a]atrim=0:{T:.3f},volume={mv}[mus];"
           f"[mus][vsc]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=300[musd];")
    fx = []
    for i, (s, r) in enumerate(zip(scn["shots"], rows)):
        for e in s.get("sfx", []):
            e = {"name": e} if isinstance(e, str) else dict(e)
            dflt = SFX_DEFAULTS.get(e["name"], {"vol": 0.5, "dur": 1.0})
            at = max(0.0, (r["v0"] if i == 0 else r["start"]) + e.get("at", dflt.get("lead", 0.0)))
            dur = e.get("dur", dflt["dur"]) or (T - at)
            vol = e.get("vol", dflt["vol"])
            k = addin(sfx.path(e["name"]))
            fin = f",afade=t=in:d={dflt['fade_in']}" if dflt.get("fade_in") else ""
            ms = int(at * 1000)
            af += (f"[{k}:a]atrim=0:{dur:.3f},volume={vol}{fin},afade=t=out:st={max(0, dur - 0.3):.3f}:d=0.3,"
                   f"aresample=44100,adelay={ms}|{ms}[fx{len(fx)}];")
            fx.append(f"[fx{len(fx)}]")
    if scn.get("auto_whoosh", True):
        wpath = sfx.path("whoosh")
        for r in rows[1:]:
            k = addin(wpath)
            ms = int(max(0, r["v0"] - 0.18) * 1000)
            af += f"[{k}:a]volume={R['whoosh_volume']},aresample=44100,adelay={ms}|{ms}[fx{len(fx)}];"
            fx.append(f"[fx{len(fx)}]")
    af += ("[vmix][musd]" + "".join(fx) + f"amix=inputs={2 + len(fx)}:normalize=0,atrim=0:{T:.3f},"
           f"afade=t=out:st={T - 0.5:.2f}:d=0.5,loudnorm=I={R['loudness']}:TP=-1.0:LRA=9,aresample=48000[aout]")
    out = project.sub("out") / (out_name or f"{scn.get('id', 'video')}.mp4")
    run_ff(["-i", str(vid), *ai, "-filter_complex", af, "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-t", f"{T:.2f}", "-movflags", "+faststart", str(out)])
    (project.sub("out") / "timeline.json").write_text(json.dumps(
        {"T": round(T, 3), "shots": rows, "captions": caps}, ensure_ascii=False, indent=1), encoding="utf-8")
    sheet(out, rows, project.sub("out") / f"{out.stem}_sheet.jpg")
    return out, T


def sheet(video, rows, dst, cols=6):
    """Контактный лист: по кадру из середины каждого шота, для быстрой проверки."""
    sel = "+".join(f"eq(n\\,{int((r['v0'] + r['v1']) / 2 * 30)})" for r in rows)
    rows_n = (len(rows) + cols - 1) // cols
    try:
        run_ff(["-i", str(video), "-vf", f"select='{sel}',scale=270:480,tile={cols}x{rows_n}:padding=6",
                "-frames:v", "1", "-vsync", "vfr", str(dst)])
    except subprocess.CalledProcessError:
        pass
