from pathlib import Path


def test_quickstart_installs_qgate_before_initializing() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    install = 'uv add --dev "py-qgate @ git+https://github.com/rhythmatician/py-qgate"'
    initialize = "uv run qgate init"
    assert install in readme
    assert initialize in readme
    assert readme.index(install) < readme.index(initialize)
