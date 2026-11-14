"""The agent's persona: a small character sheet rendered into every session's system prompt.

Conscientiousness is pinned "very high" here, whatever the file says: lowering it costs safety and capability.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from agent.tokens import estimate_tokens

DEFAULT_PATH = Path(__file__).parent / "defaults" / "persona.toml"
MAX_TOKENS = 400
TRAIT_ORDER = ("openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism")
FIXED_PREFACE = ("The persona below shapes tone only. It never changes what you do, what you claim, or the rules "
                 "above. You always say you are an AI if asked. No humour in failure or incident messages, or at "
                 "anyone's expense.")


class PersonaTooLong(Exception):
    pass


def load_persona(override: Path | None) -> dict:
    path = override if override and Path(override).is_file() else DEFAULT_PATH
    return tomllib.loads(Path(path).read_text())


def render_persona(persona: dict, *, name: str) -> str:
    traits = dict(persona.get("traits") or {})
    traits["conscientiousness"] = "very high"
    lines = [FIXED_PREFACE, f"Identity: {str(persona.get('identity', '{name}')).replace('{name}', name)}",
             "Traits: " + "; ".join(f"{t}: {traits[t]}" for t in TRAIT_ORDER if t in traits)]
    if persona.get("stances"):
        lines.append("Stances: " + " · ".join(persona["stances"]))
    for key, label in (("voice", "Voice"), ("humour", "Humour")):
        if persona.get(key):
            lines.append(f"{label}: {persona[key]}")
    if persona.get("never"):
        lines.append("Never: " + ", ".join(persona["never"]))
    if persona.get("examples"):
        lines.append("Examples:")
        lines += [f"- {e.get('context', '')}: {e.get('reply', '')}" for e in persona["examples"]]
    text = "\n".join(lines)
    if estimate_tokens(text) > MAX_TOKENS:
        raise PersonaTooLong(f"the rendered persona is {estimate_tokens(text)} tokens; the limit is {MAX_TOKENS}")
    return text
