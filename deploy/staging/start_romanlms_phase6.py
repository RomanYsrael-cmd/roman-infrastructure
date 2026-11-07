#!/usr/bin/env python3
"""Start only the previously verified private Phase 6 services after mount checks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

NETWORK = "romanlms-phase6-staging"
SERVICES = {
    "romanlms-phase6-db": ("database", "/srv/databases/romanlms-phase6/postgres", "/var/lib/postgresql/data", "postgres@sha256:123b17998ccaf47e53b502e8c6a1e79afd8f579c6f3eec83c563da026f957395", "172.30.66.10"),
    "romanlms-phase6-object": ("object", "/srv/data/romanlms-phase6/objects", "/data", "chrislusf/seaweedfs@sha256:57877e32bb90073b37dc109b438ba66e84fd5ed9ced5f5c04f56444ef243ce59", "172.30.66.11"),
}


class StartError(Exception):
    pass


def run(args: list[str], timeout: int = 30) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StartError(f"Required operation failed: {Path(args[0]).name}.") from exc
    if result.returncode:
        raise StartError(f"Required operation failed: {Path(args[0]).name}.")
    return result.stdout.strip()


def verify_service(name: str, role: str, source: str, destination: str, image: str) -> bool:
    fmt = '{{index .Config.Labels "roman.platform.staging"}}|{{index .Config.Labels "roman.platform.role"}}|{{.HostConfig.NetworkMode}}|{{.Config.Image}}|{{json .Mounts}}|{{.HostConfig.RestartPolicy.Name}}|{{json .HostConfig.PortBindings}}|{{.State.Running}}'
    fields = run(["docker", "inspect", "--format", fmt, name]).split("|", 7)
    if len(fields) != 8 or fields[:4] != ["romanlms-phase6", role, NETWORK, image] or fields[5] != "no" or fields[6] not in {"null", "{}"}:
        raise StartError("A staging dependency does not match its reserved identity.")
    try:
        mounts = json.loads(fields[4])
    except json.JSONDecodeError as exc:
        raise StartError("A staging dependency mount could not be verified.") from exc
    if not isinstance(mounts, list) or not any(
        item.get("Type") == "bind" and item.get("Source") == source and item.get("Destination") == destination and item.get("RW") is True
        for item in mounts if isinstance(item, dict)
    ) or os.path.realpath(source) != source:
        raise StartError("A staging dependency does not use its approved storage mount.")
    return fields[7].lower() == "true"


def wait_healthy(name: str, expected_ip: str) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        value = run(["docker", "inspect", "--format", '{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{json .NetworkSettings.Networks}}', name])
        health, raw_networks = value.split("|", 1)
        try:
            networks = json.loads(raw_networks)
            address = networks[NETWORK]["IPAddress"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise StartError("A staging dependency network could not be verified.") from exc
        if set(networks) != {NETWORK} or (address and address != expected_ip):
            raise StartError("A staging dependency is not on its approved private address.")
        if health == "healthy" and address == expected_ip:
            return
        time.sleep(2)
    raise StartError("A staging dependency did not become healthy.")


def main() -> int:
    if os.geteuid() != 0:
        raise StartError("The staging startup requires the existing root service.")
    for mount in ("/srv/apps", "/srv/databases", "/srv/data"):
        if not os.path.ismount(mount):
            raise StartError(f"Required storage mount is unavailable: {mount}.")
    docker_root = os.path.realpath(run(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=20))
    if not docker_root.startswith("/srv/apps/"):
        raise StartError("Docker storage is not on the applications mount.")
    if run(["docker", "network", "inspect", "--format", "{{.Internal}}", NETWORK]).lower() != "true":
        raise StartError("The staging network is not isolated.")
    try:
        ipam = json.loads(run(["docker", "network", "inspect", "--format", "{{json .IPAM.Config}}", NETWORK]))
        if {item.get("Subnet") for item in ipam} != {"172.30.66.0/24"}:
            raise StartError("The staging network subnet does not match its approved policy.")
    except (TypeError, json.JSONDecodeError) as exc:
        raise StartError("The staging network subnet could not be verified.") from exc
    for name, (role, source, destination, image, address) in SERVICES.items():
        if not verify_service(name, role, source, destination, image):
            run(["docker", "start", name], timeout=60)
        wait_healthy(name, address)
    state_path = Path("/srv/apps/romanlms-phase6/state/romanlms-phase6.json")
    if not state_path.exists():
        print("Private staging dependencies are healthy; no application release is active.")
        return 0
    sys.path.insert(0, "/opt/roman-platform/controller")
    import roman_deploy as deploy
    config = json.loads(Path("/etc/roman-platform/applications.json").read_text(encoding="utf-8"))
    app = config["applications"]["romanlms-phase6"]
    state = deploy._read_state(str(state_path))
    active = state.get("active")
    if not isinstance(active, dict) or not isinstance(active.get("components"), dict) or set(active["components"]) != set(app["components"]):
        raise StartError("The active application release state is invalid.")
    deploy.preflight(app)
    deploy._stack_validate_network(app)
    expected_route = deploy._stack_route_content(app["components"], active.get("slot"))
    if Path(app["route_file"]).read_text(encoding="utf-8") != expected_route:
        raise StartError("The private route does not match the active release state.")
    for name, record in sorted(active["components"].items()):
        component = deploy._stack_component_app(app, name)
        if not deploy._release_container_available(component, record):
            raise StartError("An active application component has an invalid identity.")
        if run(["docker", "inspect", "--format", "{{.State.Running}}", record["container"]]).lower() != "true":
            run(["docker", "start", record["container"]], timeout=60)
    deploy._stack_release_healthy(app, active)
    for name in sorted(app["components"]):
        component = deploy._stack_component_app(app, name)
        deploy._wait_http(component["route_health_url"], component["expected_health_text"], int(component["readiness_timeout_seconds"]))
    print("Private staging dependencies and the recorded active application are healthy.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Private staging startup failed: {type(exc).__name__}.", file=sys.stderr)
        raise SystemExit(1)
