#!/usr/bin/env python3
"""Build the exact adapter image referenced by compose.yml and publish it."""

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def image_settings(env_file):
    # Let Compose parse .env quoting/interpolation; never source it as shell code.
    # The resolved model contains secrets, so it stays in memory and is never logged.
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", str(ROOT / "compose.yml"),
         "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode:
        raise ValueError("Compose configuration is invalid. Fill in .env and run docker compose config --quiet.")
    model = json.loads(result.stdout)
    image = model["services"]["connexis-adapter"]["image"]
    if "your-dockerhub-username" in image:
        raise ValueError("Replace DOCKERHUB_NAMESPACE in .env with your Docker Hub username or organization.")
    return image, model.get("x-build-platforms", "linux/amd64,linux/arm64")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--platforms", help="Override BUILD_PLATFORMS, e.g. linux/amd64")
    parser.add_argument("--builder", default="traccar-adapter-builder", help="Named Buildx docker-container builder")
    parser.add_argument("--dry-run", action="store_true", help="Show the image and build command without building or pushing")
    parser.add_argument("--load", action="store_true", help="Load a single-platform build locally instead of pushing")
    args = parser.parse_args()
    if not args.env_file.is_file():
        parser.error("Environment file is missing. Copy .env.example to .env and configure it first.")
    try:
        image, defaults = image_settings(args.env_file.resolve())
        platforms = args.platforms or defaults
        if args.load and "," in platforms:
            parser.error("--load needs one platform; supply --platforms linux/amd64 or linux/arm64")
        command = [
            "docker", "buildx", "build", "--builder", args.builder,
            "--platform", platforms, "--pull", "--tag", image,
            "--file", str(ROOT / "Dockerfile"),
            "--load" if args.load else "--push", str(ROOT),
        ]
        print(f"Adapter image: {image}", flush=True)
        print(shlex.join(command), flush=True)
        if args.dry_run:
            return 0
        # Do not change the user's currently selected builder.
        found = subprocess.run(["docker", "buildx", "inspect", args.builder],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if found.returncode:
            subprocess.run(["docker", "buildx", "create", "--name", args.builder,
                            "--driver", "docker-container"], check=True)
        subprocess.run(["docker", "buildx", "inspect", "--bootstrap", args.builder], check=True)
        subprocess.run(command, cwd=ROOT, check=True)
        print(f"{'Loaded locally' if args.load else 'Published'}: {image}")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Build setup failed: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        print("Docker build/push failed. Check the output above, registry login, and repository permissions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
