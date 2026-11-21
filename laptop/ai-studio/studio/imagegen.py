"""Кадры. mode=api: Gemini (Nano Banana) по API. mode=manual: промпты в tasks/flow_tasks.md, кадры кладёте сами."""
from . import prompts
from .config import secret


def _client(cfg):
    from google import genai
    return genai.Client(api_key=secret(cfg["image"]))


def generate_one(cfg, prompt, ref_paths, out_path, aspect=None):
    from google.genai import types
    from PIL import Image

    client = _client(cfg)
    contents = [prompt] + [Image.open(p) for p in ref_paths]
    kw = {"response_modalities": ["IMAGE"]}
    try:
        conf = types.GenerateContentConfig(**kw, image_config=types.ImageConfig(aspect_ratio=aspect or cfg["image"]["aspect_ratio"]))
    except Exception:  # старые версии SDK
        conf = types.GenerateContentConfig(**kw)
    resp = client.models.generate_content(model=cfg["image"]["model"], contents=contents, config=conf)
    for cand in resp.candidates or []:
        for part in cand.content.parts or []:
            if getattr(part, "inline_data", None) and part.inline_data.data:
                out_path.write_bytes(part.inline_data.data)
                return out_path
    raise RuntimeError(f"Модель не вернула картинку (возможно, отказ по политике): {resp}")


def run(project, cfg, only=None, force=False):
    scn = project.load()
    tasks = prompts.write_tasks(project, scn)
    mode = cfg["image"]["mode"]
    if mode == "manual":
        missing = [c for c in scn["clips"] if not project.find_frame(scn, c) and not project.find_clip(scn, c)]
        print(f"Ручной режим. Промпты: {tasks}")
        print("Нет кадров для:", ", ".join(missing) if missing else "всё готово")
        return
    for cid in scn["clips"]:
        if only and cid not in only:
            continue
        if project.find_frame(scn, cid) and not force:
            print(f"{cid}: кадр уже есть")
            continue
        out = project.sub("frames") / f"{cid}.png"
        print(f"{cid}: генерирую кадр...")
        try:
            generate_one(cfg, prompts.frame_prompt(scn, cid), prompts.refs(scn, cid), out, scn.get("aspect"))
            print(f"{cid}: ок -> {out.name}")
        except Exception as e:  # noqa
            print(f"{cid}: ОШИБКА {e}")
