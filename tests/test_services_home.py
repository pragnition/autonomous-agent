from pathlib import Path

from agent.services.home import prepare


def _image(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A volume root, the image's /etc/skel and the image's browsers, as the Dockerfile leaves them."""
    root, skel, browsers = tmp_path / "var-agent", tmp_path / "skel", tmp_path / "ms-playwright-image"
    root.mkdir()
    skel.mkdir()
    (skel / ".bashrc").write_text("# image bashrc\n")
    (skel / ".profile").write_text("# image profile\n")
    (browsers / "chromium-1200" / "chrome-linux").mkdir(parents=True)
    (browsers / "chromium-1200" / "chrome-linux" / "chrome").write_text("image chromium")
    (browsers / "ffmpeg-1013").mkdir()
    (browsers / "ffmpeg-1013" / "ffmpeg").write_text("image ffmpeg")
    (browsers / ".links").mkdir()
    (browsers / ".links" / "image-link").write_text("/usr/local/lib/node_modules/@playwright/cli")
    return root, skel, browsers


def test_a_fresh_volume_gets_a_home_from_skel_with_the_claude_link(tmp_path):
    root, skel, browsers = _image(tmp_path)

    prepare(root, skel=skel, browsers=browsers)

    home = root / "home"
    assert (home / ".bashrc").read_text() == "# image bashrc\n"
    assert (home / ".profile").read_text() == "# image profile\n"
    assert (home / ".claude").is_symlink() and (home / ".claude").readlink() == root / "claude"


def test_an_existing_home_is_left_as_the_agent_left_it(tmp_path):
    root, skel, browsers = _image(tmp_path)
    home = root / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "graphify-mcp").write_text("tool")
    (home / ".bashrc").write_text("# image bashrc\n. \"$HOME/.local/bin/env\"\n")
    (home / ".claude").symlink_to(root / "claude")

    prepare(root, skel=skel, browsers=browsers)

    assert (home / ".bashrc").read_text() == "# image bashrc\n. \"$HOME/.local/bin/env\"\n"
    assert (home / ".local" / "bin" / "graphify-mcp").read_text() == "tool"
    assert not (home / ".profile").exists()  # skel only seeds a home that doesn't exist yet
    assert (home / ".claude").readlink() == root / "claude"


def test_a_home_without_the_claude_link_gets_one(tmp_path):
    root, skel, browsers = _image(tmp_path)
    (root / "home").mkdir()

    prepare(root, skel=skel, browsers=browsers)

    assert (root / "home" / ".claude").readlink() == root / "claude"


def test_the_images_browsers_are_copied_in_without_touching_the_agents(tmp_path):
    root, skel, browsers = _image(tmp_path)
    dest = root / "ms-playwright"
    (dest / "chromium-1255").mkdir(parents=True)
    (dest / "chromium-1255" / "chrome").write_text("agent's chromium")
    (dest / "ffmpeg-1013").mkdir()
    (dest / "ffmpeg-1013" / "ffmpeg").write_text("already here")
    (dest / ".links").mkdir()
    (dest / ".links" / "agent-link").write_text("/var/agent/scratch/app")

    prepare(root, skel=skel, browsers=browsers)

    assert (dest / "chromium-1200" / "chrome-linux" / "chrome").read_text() == "image chromium"
    assert (dest / "chromium-1255" / "chrome").read_text() == "agent's chromium"
    assert (dest / "ffmpeg-1013" / "ffmpeg").read_text() == "already here"
    assert sorted(p.name for p in (dest / ".links").iterdir()) == ["agent-link", "image-link"]
    assert not [p for p in dest.iterdir() if p.name.startswith(".copying-")]


def test_no_browsers_in_the_image_still_makes_the_folder(tmp_path):
    root, skel, _ = _image(tmp_path)

    prepare(root, skel=skel, browsers=tmp_path / "missing")

    assert (root / "ms-playwright").is_dir()


def test_a_browser_copy_that_died_halfway_is_redone(tmp_path):
    root, skel, browsers = _image(tmp_path)
    leftover = root / "ms-playwright" / ".copying-chromium-1200"
    leftover.mkdir(parents=True)
    (leftover / "partial").write_text("half")

    prepare(root, skel=skel, browsers=browsers)

    assert (root / "ms-playwright" / "chromium-1200" / "chrome-linux" / "chrome").read_text() == "image chromium"
    assert not leftover.exists()


def test_a_failure_is_logged_and_never_stops_the_services(tmp_path, caplog):
    root, skel, browsers = _image(tmp_path)
    (root / "home").write_text("a file where the home folder should be")

    prepare(root, skel=skel, browsers=browsers)

    assert "home folder" in caplog.text
    assert (root / "ms-playwright" / "chromium-1200").is_dir()  # the browsers still get done
