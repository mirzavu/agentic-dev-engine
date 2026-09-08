from __future__ import annotations

import os
import platform
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


def docker_platform() -> str:
    return "linux/arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "linux/amd64"


def available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def workspace_mount(workspace: Path) -> str:
    return f"{workspace.resolve()}:/workspace:rw"


class LoopbackDockerWorkspace:  # loaded lazily so offline tests need no OpenHands import
    """SDK DockerWorkspace with its required control port bound only to loopback."""

    @staticmethod
    def create(workspace: Path, *, health_timeout: float = 120.0, extra_volumes: list[str] | None = None):
        from openhands.workspace import DockerWorkspace

        class _LoopbackDockerWorkspace(DockerWorkspace):
            def _wait_for_health(self, *, timeout: float) -> None:
                # Avoid the SDK poller's premature container inspection; this
                # image can accept its first HTTP connection a few seconds late.
                import httpx

                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        response = httpx.get(f"{self.host.rstrip('/')}/health", timeout=2.0)
                        if response.is_success:
                            return
                    except httpx.HTTPError:
                        pass
                    time.sleep(1)
                raise RuntimeError("Docker workspace failed its loopback health check")

            def _start_container(self, image: str, context: Any) -> None:  # noqa: ANN401
                # The SDK currently lacks a host-bind field. This is its implementation
                # with only the published port changed to 127.0.0.1.
                from openhands.workspace.docker.workspace import check_port_available

                self._image_name = image
                self.host_port = int(self.host_port or available_loopback_port())
                if not check_port_available(self.host_port):
                    raise RuntimeError(f"Port {self.host_port} is not available")
                if subprocess.run(["docker", "version"], capture_output=True, check=False).returncode:
                    raise RuntimeError("Docker is unavailable")
                flags: list[str] = []
                for volume in self.volumes:
                    flags += ["-v", volume]
                # ACP conversation state can contain a materialized provider
                # credential. Keep all server state inside the ephemeral
                # container, never in the generated application mount.
                flags += [
                    "-e", "OH_CONVERSATIONS_PATH=/tmp/autodev/conversations",
                    "-e", "OH_BASH_EVENTS_DIR=/tmp/autodev/bash-events",
                ]
                flags += ["-p", f"127.0.0.1:{self.host_port}:8000"]
                command = [
                    "docker", "run", "-d", "--platform", self.platform, "--rm",
                    "--ulimit", "nofile=65536:65536", "--name", f"autodev-sandbox-{uuid.uuid4()}",
                    *flags, image, "--host", "0.0.0.0", "--port", "8000",
                ]
                result = subprocess.run(command, text=True, capture_output=True, check=False)
                if result.returncode:
                    raise RuntimeError(f"Failed to run Docker workspace: {result.stderr.strip()}")
                self._container_id = result.stdout.strip()
                # Current SDK DockerWorkspace assigns host after _wait_for_health,
                # although that check derives its URL from host. Set it first.
                object.__setattr__(self, "host", f"http://127.0.0.1:{self.host_port}")
                object.__setattr__(self, "api_key", None)
                self._wait_for_health(timeout=self.health_check_timeout)
                super(DockerWorkspace, self).model_post_init(context)

        return _LoopbackDockerWorkspace(
            working_dir="/workspace",
            volumes=[workspace_mount(workspace), *(extra_volumes or [])],
            forward_env=[],
            extra_ports=False,
            detach_logs=True,
            platform=docker_platform(),
            health_check_timeout=health_timeout,
        )


def normalize_permissions(workspace: Path) -> None:
    """Fix container-created ownership without following hostile symlinks."""
    uid, gid = os.getuid(), os.getgid()
    mount = workspace_mount(workspace)
    script = (
        "find -P /workspace -xdev -exec chown -h \"$1:$2\" {} + && "
        "find -P /workspace -xdev -type d -exec chmod u+rwx,go-rwx {} + && "
        "find -P /workspace -xdev -type f -exec chmod u+rw,go-rwx {} + && "
        # npm package archives do not reliably preserve executable bits across
        # differing container/host users. Restore them only in dependency bin
        # directories, never across arbitrary application source files.
        "if [ -d /workspace/node_modules ]; then "
        "find -P /workspace/node_modules -type f -path '*/bin/*' -exec chmod u+rwx,go-rwx {} +; "
        "fi"
    )
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--user", "0:0", "-v", mount,
         "alpine:3.20", "sh", "-c", script, "normalizer", str(uid), str(gid)],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Workspace ownership normalization failed: {result.stderr.strip()}")
    for path in [workspace, *workspace.rglob("*")]:
        if not os.access(path, os.R_OK | os.W_OK):
            raise RuntimeError(f"Workspace remains inaccessible: {path}")
