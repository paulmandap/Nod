"""Entry point for the packaged build.

PyInstaller freezes a script, and there is no equivalent of `python -m
copilot.main`. This is that script, and it is deliberately three lines: every
piece of startup ordering that matters -- the stdio shim, config.apply_env, the
COM apartment -- lives at the top of copilot/main.py, where it also applies when
running from source. Duplicating any of it here would mean two startup paths
that drift apart.

    python nod_main.py            # identical to python -m copilot.main
"""

from copilot.main import main

if __name__ == "__main__":
    raise SystemExit(main())
