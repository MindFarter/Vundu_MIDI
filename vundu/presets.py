"""Preset library: factory presets (presets/factory, shipped with the
repo) and user presets (data/presets, created when you save)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    return s or f"preset_{int(time.time())}"


class PresetStore:
    def __init__(self, factory_dir: Path, user_dir: Path):
        self.factory_dir = factory_dir
        self.user_dir = user_dir
        user_dir.mkdir(parents=True, exist_ok=True)

    def _read(self, path: Path, origin: str) -> dict | None:
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None
        data["id"] = f"{origin}:{path.stem}"
        data["origin"] = origin
        data.setdefault("name", path.stem.replace("_", " ").title())
        data.setdefault("category", "General")
        return data

    def all(self) -> list[dict]:
        out = []
        for origin, folder in (("factory", self.factory_dir), ("user", self.user_dir)):
            for f in sorted(folder.glob("*.json")):
                p = self._read(f, origin)
                if p:
                    out.append(p)
        return out

    def summary(self) -> list[dict]:
        return [{"id": p["id"], "name": p["name"], "category": p["category"], "origin": p["origin"],
                 "instrument": p.get("instrument"), "description": p.get("description", ""),
                 "effects": [c.get("name") or c.get("type") for c in p.get("chain", [])]}
                for p in self.all()]

    def get(self, preset_id: str) -> dict:
        origin, _, stem = preset_id.partition(":")
        folder = self.factory_dir if origin == "factory" else self.user_dir
        path = folder / f"{stem}.json"
        if not path.exists() or "/" in stem or ".." in stem:
            raise KeyError(preset_id)
        return self._read(path, origin)

    def by_index(self, index: int) -> dict:
        items = self.all()
        return items[index % len(items)]

    def save(self, name: str, category: str, data: dict, description: str = "") -> dict:
        stem = slugify(name)
        doc = {"name": name, "category": category or "User", "description": description,
               "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), **data}
        (self.user_dir / f"{stem}.json").write_text(json.dumps(doc, indent=2))
        return self.get(f"user:{stem}")

    def delete(self, preset_id: str):
        origin, _, stem = preset_id.partition(":")
        if origin != "user":
            raise PermissionError("factory presets cannot be deleted")
        path = self.user_dir / f"{stem}.json"
        if not path.exists() or "/" in stem or ".." in stem:
            raise KeyError(preset_id)
        path.unlink()
