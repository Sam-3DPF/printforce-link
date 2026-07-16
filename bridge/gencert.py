"""Generate the self-signed TLS cert the print-host serves (setup helper).

The print-host is HTTPS because an accepted upload lands next to config.toml's
access codes; OrcaSlicer needs a cert to trust. This shells out to `openssl`
(present on macOS/Linux and the mini-PCs the bridge runs on) rather than pulling
in a crypto dependency the runtime never needs. Setup-time only.

    python -m bridge.gencert                 # CN/SAN = 127.0.0.1
    python -m bridge.gencert --host 192.168.86.20
"""

import argparse
import ipaddress
import os
import subprocess
import sys


def _san_for(host: str) -> str:
    """Build the subjectAltName for `host`.

    openssl REJECTS `IP:<hostname>` ("bad ip address") — so the SAN type has to
    match what `host` actually is. An IP goes in `IP:`, anything else in `DNS:`;
    emitting both unconditionally hard-fails cert generation for every non-IP
    host (`--host myhost.local`).
    """
    try:
        ipaddress.ip_address(host)
        return f"subjectAltName=IP:{host}"
    except ValueError:
        return f"subjectAltName=DNS:{host}"


def generate(host: str, out_dir: str, days: int = 3650) -> tuple:
    """Write a self-signed cert+key for `host` into `out_dir`. Returns their paths."""
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    crt = os.path.join(out_dir, "printhost.crt")
    key = os.path.join(out_dir, "printhost.key")
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key, "-out", crt, "-days", str(days),
        "-subj", f"/CN={host}", "-addext", _san_for(host),
    ]
    # Tighten umask around the write so the private key is never world-readable,
    # even for the instant between openssl creating it and the chmod below.
    old_umask = os.umask(0o077)
    try:
        subprocess.run(cmd, check=True)
    finally:
        os.umask(old_umask)
    # The key is a secret; keep it owner-only like config.toml.
    os.chmod(key, 0o600)
    return crt, key


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate the print-host TLS cert.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="hostname or IP OrcaSlicer connects to (default 127.0.0.1)")
    parser.add_argument("--out", default="certs", help="output directory (default certs/)")
    args = parser.parse_args(argv)
    try:
        crt, key = generate(args.host, args.out)
    except FileNotFoundError:
        print("openssl not found — install it, or generate certs/printhost.{crt,key} "
              "another way.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"openssl failed: {e}", file=sys.stderr)
        return 1
    print(f"Wrote {crt} and {key} (host={args.host}).")
    print("Point [printhost] cert_file/key_file at these, and trust the .crt in OrcaSlicer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
