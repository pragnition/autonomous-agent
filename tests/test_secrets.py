from agent.secrets import load_secrets


def test_inline_comment_is_not_part_of_the_value(tmp_path):
    f = tmp_path / "core.env"
    f.write_text('LINEAR_WEBHOOK_SECRET=lin_wh_abc      # the webhook "Signing secret"\n'
                 'QUOTED="has # inside"  # note\n'
                 "HASH_NO_SPACE=abc#def\n")
    assert load_secrets(f) == {"LINEAR_WEBHOOK_SECRET": "lin_wh_abc", "QUOTED": "has # inside",
                               "HASH_NO_SPACE": "abc#def"}
