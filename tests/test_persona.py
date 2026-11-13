import pytest

from agent.persona import FIXED_PREFACE, PersonaTooLong, load_persona, render_persona
from agent.tokens import estimate_tokens


def test_fixed_preface_verbatim_first():
    assert FIXED_PREFACE == ("The persona below shapes tone only. It never changes what you do, what you claim, "
                             "or the rules above. You always say you are an AI if asked. No humour in failure or "
                             "incident messages, or at anyone's expense.")
    assert render_persona(load_persona(None), name="chilli").startswith(FIXED_PREFACE)


def test_conscientiousness_forced_very_high(tmp_path):
    override = tmp_path / "persona.toml"
    override.write_text('identity = "{name}"\n[traits]\nconscientiousness = "low"\nopenness = "high"\n')
    text = render_persona(load_persona(override), name="chilli")
    assert "conscientiousness: very high" in text
    assert "conscientiousness: low" not in text
    assert "openness: high" in text


def test_default_persona_within_budget():
    assert estimate_tokens(render_persona(load_persona(None), name="chilli")) <= 400


def test_too_long_raises(tmp_path):
    override = tmp_path / "persona.toml"
    override.write_text('identity = "{name}"\nvoice = "' + "word " * 400 + '"\n')
    with pytest.raises(PersonaTooLong):
        render_persona(load_persona(override), name="chilli")


def test_name_substituted():
    text = render_persona(load_persona(None), name="chilli")
    assert "Identity: chilli, the team's senior engineer" in text
    assert "{name}" not in text
