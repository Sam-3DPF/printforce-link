"""PyInstaller entrypoint for the packaged PrintForce Link agent (U7).

Resolves config.toml relative to the install ROOT — the parent of the --onedir folder —
so config.toml and printers.json survive a self-update that swaps the executable folder.
Points TLS at the bundled CA bundle (the classic frozen-app gotcha for paho/httpx).
"""
import os
import sys


def _config_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    if getattr(sys, "frozen", False):
        # <root>/printforce-link/<exe>  ->  <root>/config.toml
        return os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "config.toml")
    return "config.toml"


def _ensure_ca_bundle() -> None:
    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except Exception:
        pass


def run() -> None:
    _ensure_ca_bundle()
    from bridge.app import main
    main(_config_path())


if __name__ == "__main__":
    run()
