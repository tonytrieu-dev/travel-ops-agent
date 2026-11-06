"""Issue one local demo device certificate; retain the CA offline after issuance."""
import argparse
import os
from pathlib import Path
import re
import secrets
import subprocess


def run(*arguments: str) -> None:
    subprocess.run(["openssl", *arguments], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device_id")
    arguments = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", arguments.device_id):
        parser.error("device ID must contain 1–64 letters, digits or hyphens")
    if arguments.device_id in {"ca", "server"}:
        parser.error("ca and server are reserved names")
    os.umask(0o077)
    directory = Path(".device-certificates")
    directory.mkdir(mode=0o700, exist_ok=True)
    if not (directory / "ca.key").exists():
        run("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "365",
            "-subj", "/CN=TravelOps device authority", "-keyout", str(directory / "ca.key"), "-out", str(directory / "ca.crt"))
    for name, subject, usage in (("server", "localhost", "serverAuth"), (arguments.device_id, arguments.device_id, "clientAuth")):
        key = directory / f"{name}.key"
        if key.exists():
            if name == "server":
                continue
            parser.error("device already issued; refusing to overwrite")
        extension = directory / f"{name}.extensions"
        extension.write_text(f"extendedKeyUsage={usage}\n" + ("subjectAltName=DNS:localhost\n" if name == "server" else ""))
        run("req", "-new", "-newkey", "rsa:3072", "-nodes", "-subj", f"/CN={subject}", "-keyout", str(key), "-out", str(directory / f"{name}.csr"))
        run("x509", "-req", "-in", str(directory / f"{name}.csr"), "-CA", str(directory / "ca.crt"), "-CAkey", str(directory / "ca.key"), "-CAcreateserial", "-days", "7", "-extfile", str(extension), "-out", str(directory / f"{name}.crt"))
    password = directory / f"{arguments.device_id}.password"
    password.write_text(secrets.token_urlsafe(24))
    run("pkcs12", "-export", "-inkey", str(directory / f"{arguments.device_id}.key"), "-in", str(directory / f"{arguments.device_id}.crt"), "-certfile", str(directory / "ca.crt"), "-out", str(directory / f"{arguments.device_id}.p12"), "-passout", f"file:{password}")
    print("Certificate issued. Import the device .p12 using its private .password file.")


if __name__ == "__main__":
    main()
