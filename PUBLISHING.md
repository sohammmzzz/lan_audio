# Publishing lan-audio to PyPI

1. Put `lanaudio.py` (v1.2.0) next to `pyproject.toml`, `README.md`, `LICENSE`.
2. Create a public GitHub repo (e.g. `lan-audio`) and push everything, including `docs/`.
3. Replace `YOUR-GITHUB-USER` in `README.md` and `pyproject.toml` with your GitHub username.
   PyPI can't host images, so the screenshots load from GitHub's raw URLs.
4. Check the name `lanaudio` is free on https://pypi.org/project/lanaudio/ . If taken, change
   `name` in pyproject.toml and the `pip install` lines in the README (the command stays `lanaudio`).
5. Build and check:

       python -m pip install -U build twine
       python -m build
       python -m twine check dist/*

6. Try TestPyPI first, then the real thing:

       python -m twine upload --repository testpypi dist/*
       pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple lanaudio
       python -m twine upload dist/*

Bump `version` in pyproject.toml AND `VERSION` in lanaudio.py together for each release.

Privacy: never commit your own config (`%APPDATA%\lanaudio\config.json`) or real tokens.
They contain IP addresses and pass secrets.
