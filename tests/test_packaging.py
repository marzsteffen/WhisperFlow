from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _checkout_pkgbuild() -> str:
    path = ROOT / "packaging/PKGBUILD"
    if not path.exists():
        pytest.skip("PKGBUILD is intentionally outside the Python sdist")
    return path.read_text(encoding="utf-8")


def test_user_service_keeps_required_device_and_hotplug_access() -> None:
    service = (ROOT / "assets/local-dictation.service").read_text(encoding="utf-8")

    # PrivateDevices would replace /dev and hide both evdev and uinput.
    assert "PrivateDevices=yes" not in service
    # The packaged ydotool.service may expose its checked fallback socket in
    # /tmp; a private /tmp would make that service permanently unreachable.
    assert "PrivateTmp=yes" not in service
    families = next(
        line.split("=", 1)[1].split()
        for line in service.splitlines()
        if line.startswith("RestrictAddressFamilies=")
    )
    assert {"AF_UNIX", "AF_INET", "AF_INET6", "AF_NETLINK"} <= set(families)


def test_user_service_releases_children_with_graphical_session() -> None:
    service = (ROOT / "assets/local-dictation.service").read_text(encoding="utf-8")

    assert "PartOf=graphical-session.target" in service
    assert "Requisite=graphical-session.target" in service
    assert "After=graphical-session.target" in service
    assert "KillMode=control-group" in service
    assert "Restart=no" in service
    assert "RuntimeDirectory=local-dictation" in service
    assert "RuntimeDirectoryMode=0700" in service
    assert "LimitCORE=0" in service


def test_kde_autostart_starts_only_the_user_service() -> None:
    desktop = (ROOT / "assets/local-dictation-autostart.desktop").read_text(
        encoding="utf-8"
    )

    assert "Exec=systemctl --user start local-dictation.service" in desktop
    assert "OnlyShowIn=KDE;" in desktop
    assert "Terminal=false" in desktop


def test_package_declares_raw_input_and_insertion_dependencies() -> None:
    pkgbuild = _checkout_pkgbuild()

    for package in ("python-evdev", "python-pyudev", "ydotool", "wl-clipboard"):
        assert f"'{package}" in pkgbuild
    assert "optdepends=('kdotool:" in pkgbuild


def test_python_arch_source_layout_and_fixtures_are_packaged() -> None:
    pkgbuild = _checkout_pkgbuild()
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    # setuptools normalizes the extracted sdist directory to an underscore.
    assert '_source_dir="local_dictation-${pkgver}"' in pkgbuild
    assert (ROOT / "src/local_dictation/diagnostics.py").is_file()
    assert "src/local_dictation/resources *.wav *.json" in manifest
    assert 'local_dictation = ["resources/*.wav", "resources/*.json"]' in pyproject


def test_release_builder_normalizes_sdist_and_updates_checksum() -> None:
    script = (ROOT / "scripts/build-arch-package").read_text(encoding="utf-8")

    assert "python -m build --sdist --no-isolation" in script
    assert "--sort=name" in script
    assert '--mtime="@$SOURCE_DATE_EPOCH"' in script
    assert "--owner=0" in script
    assert "gzip -n -9" in script
    assert "updpkgsums" in script
    assert "makepkg --verifysource" in script


def test_documentation_explains_login_group_refresh() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "sudo usermod -aG input" in readme
    assert "vollständige Ab- und Anmeldung" in readme
    assert "/dev/uinput" in readme
