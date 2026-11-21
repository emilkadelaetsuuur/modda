"""Папка проекта ролика.

<проект>/
  scenario.json   сценарий (пишет LLM или человек)
  law.txt         текст закона (lex.uz)
  frames/         кадры <clip_id>.png|jpg
  clips/          видео  <clip_id>.mp4
  voice/          озвучка по шотам <shot_id>.wav + manifest.json
  tasks/          промпты для ручной генерации (Flow)
  work/           временные файлы сборки
  out/            готовый ролик
"""
import json
from pathlib import Path


class Project:
    def __init__(self, path):
        self.dir = Path(path).resolve()
        for d in ("frames", "clips", "voice", "tasks", "work", "out"):
            (self.dir / d).mkdir(parents=True, exist_ok=True)

    # пути
    @property
    def scenario_path(self):
        return self.dir / "scenario.json"

    @property
    def law_path(self):
        return self.dir / "law.txt"

    def sub(self, name):
        return self.dir / name

    # сценарий
    def load(self):
        if not self.scenario_path.exists():
            raise FileNotFoundError(f"Нет {self.scenario_path}. Сначала: python -m studio script {self.dir}")
        return json.loads(self.scenario_path.read_text(encoding="utf-8"))

    def save(self, scn):
        self.scenario_path.write_text(json.dumps(scn, ensure_ascii=False, indent=2), encoding="utf-8")

    def law(self):
        return self.law_path.read_text(encoding="utf-8") if self.law_path.exists() else ""

    # где искать медиа клипа (можно указать clips_dir / frames_dir в сценарии)
    def _dirs(self, scn, key, default):
        extra = scn.get(key)
        dirs = [self.dir / default]
        if extra:
            dirs.insert(0, (self.dir / extra).resolve())
        return dirs

    def find_frame(self, scn, clip_id):
        for d in self._dirs(scn, "frames_dir", "frames"):
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                p = d / f"{clip_id}{ext}"
                if p.exists():
                    return p
        return None

    def find_clip(self, scn, clip_id):
        for d in self._dirs(scn, "clips_dir", "clips"):
            p = d / f"{clip_id}.mp4"
            if p.exists():
                return p
        return None
