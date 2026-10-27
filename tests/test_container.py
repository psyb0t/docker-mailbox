"""Production image startup, least privilege and persistent operational state."""

import json
import shutil
import subprocess
import time
import tomllib
import uuid
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.docker


def test_compose_examples_render_without_real_credentials() -> None:
    work = Path.cwd() / ".testing" / ("compose-" + uuid.uuid4().hex)
    work.mkdir(parents=True)
    for source, target in (
        ("docker-compose.yml", "docker-compose.yml"),
        ("config.example.yaml", "config.yaml"),
        (".env.example", ".env"),
    ):
        shutil.copy2(source, work / target)
    rendered = docker(
        "compose",
        "--project-directory",
        str(work),
        "--env-file",
        str(work / ".env"),
        "--profile",
        "watch",
        "-f",
        str(work / "docker-compose.yml"),
        "config",
        "--format",
        "json",
    )
    (work / "rendered.json").write_text(rendered)
    document = json.loads(rendered)
    assert set(document["services"]) == {"mailboxd", "watch"}
    for service in document["services"].values():
        assert service["read_only"] and service["init"]
        assert service["pids_limit"] == service["deploy"]["resources"]["limits"]["pids"] == 256


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    return result.stdout.strip()


PROBE = """
import json, socket, sys, urllib.error, urllib.request
path, token, data = sys.argv[1:]
url = 'http://' + socket.gethostbyname(socket.gethostname()) + ':8000' + path
headers = {'Authorization': 'Bearer ' + token} if token else {}
if data:
    headers['Content-Type'] = 'application/json'
request = urllib.request.Request(url, headers=headers, data=data.encode() if data else None)
try:
    response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3)
except urllib.error.HTTPError as error:
    response = error
except urllib.error.URLError:
    print(json.dumps({'status': 0}))
    sys.exit(0)
with response:
    print(json.dumps({'status': response.status, 'body': json.load(response)}))
"""


def request(name: str, path: str, token: str = "", data: dict | None = None) -> dict:
    return json.loads(
        docker(
            "exec",
            name,
            "python",
            "-c",
            PROBE,
            path,
            token,
            json.dumps(data) if data is not None else "",
        )
    )


def wait_ready(name: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if request(name, "/health")["status"] == 200:
            return
        time.sleep(0.1)
    pytest.fail("production container did not become ready")


def test_production_state_survives_restart() -> None:
    root = Path.cwd()
    run_id = uuid.uuid4().hex
    work = root / ".testing" / ("container-" + run_id)
    work.mkdir(parents=True)
    config = work / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "auth": {
                    "tokens": [
                        {"name": "reader", "token": "EXAMPLE-DO-NOT-USE-read", "scopes": ["read"]},
                        {"name": "admin", "token": "EXAMPLE-DO-NOT-USE-admin", "scopes": ["admin"]},
                    ]
                },
                "mailboxes": [
                    {
                        "name": "m",
                        "smtp": {
                            "host": "smtp.invalid",
                            "username": "u",
                            "password": "REPLACE_ME",
                            "from_address": "sender@example.org",
                        },
                    }
                ],
            }
        )
    )
    name = "mailboxd-integration-" + run_id
    label = "mailboxd.test=" + run_id
    started = False
    try:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            label,
            "--read-only",
            "--init",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "256",
            "-p",
            "127.0.0.1::8000",
            "--mount",
            f"type=bind,src={config},dst=/etc/mailboxd/config.yaml,readonly",
            "psyb0t/mailbox:latest",
        )
        started = True
        assert docker("port", name, "8000/tcp").startswith("127.0.0.1:")
        wait_ready(name)
        expected_version = tomllib.loads((root / "pyproject.toml").read_text())["project"][
            "version"
        ]
        assert request(name, "/health")["body"]["version"] == expected_version
        assert request(name, "/mailboxes")["status"] == 401
        response = request(
            name,
            "/mailboxes/m/send",
            "EXAMPLE-DO-NOT-USE-read",
            {
                "to": ["recipient@example.org"],
                "subject": "test",
                "body_text": "test",
            },
        )
        assert response["status"] == 403
        before = request(name, "/audit", "EXAMPLE-DO-NOT-USE-admin")["body"]["entries"]
        assert before and before[0]["outcome"] == "denied"
        docker("restart", name)
        wait_ready(name)
        assert request(name, "/audit", "EXAMPLE-DO-NOT-USE-admin")["body"]["entries"] == before
        info = json.loads(docker("inspect", name))[0]
        assert info["Config"]["User"] == "mailboxd"
        assert info["HostConfig"]["ReadonlyRootfs"] is True
        assert "ALL" in info["HostConfig"]["CapDrop"]
    finally:
        if started:
            info = json.loads(docker("inspect", name))[0]
            assert info["Config"]["Labels"]["mailboxd.test"] == run_id
            (work / "container.log").write_text(docker("logs", name))
            docker("rm", "-f", "-v", name)
