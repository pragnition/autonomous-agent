import tomllib

from agent.dashboard.names import name_from_email, write_account_names

PROXY = """port = 18088

[accounts]
"sam.taylor@example.com" = {code = "badger"}
"jane.q.public@example.org" = {code = "ivory"}
"casey.jordan@example.com" = "quartz"
"broken@example.org" = {disabled = true}
"""


def test_name_from_email():
    assert name_from_email("sam.taylor@example.com") == "Sam Taylor"
    assert name_from_email("jane.q.public@example.org") == "Jane Q Public"
    assert name_from_email("casey.jordan@example.com") == "Casey Jordan"
    assert name_from_email("ann_lee-tan@x.com") == "Ann Lee Tan"
    assert name_from_email("not-an-email") == "Not An Email"


def test_write_account_names(tmp_path):
    proxy, out = tmp_path / "proxy.toml", tmp_path / "account-names.toml"
    proxy.write_text(PROXY)
    assert write_account_names(proxy, out) == 3
    names = tomllib.loads(out.read_text())["accounts"]
    assert names["badger"] == {"name": "Sam Taylor", "email": "sam.taylor@example.com"}
    assert names["ivory"]["name"] == "Jane Q Public"
    assert names["quartz"]["name"] == "Casey Jordan"
    assert "broken@example.org" not in out.read_text()
