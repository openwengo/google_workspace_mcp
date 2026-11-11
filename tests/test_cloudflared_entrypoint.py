"""Entrypoint of the cloudflared image (Dockerfile.cloudflared): the tunnel token goes
from SSM to a private file, then cloudflared replaces the script. The token is never in
the environment or in a message."""

import importlib.util
import os
import stat
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "cloudflared" / "entrypoint.py"
)
PARAMETER = "exploit.cloudflared_mcp_portal.tunnel_token"
TOKEN = "eyJhIjoiMDAwIiwidCI6IjAwMSIsInMiOiJ4eHgifQ=="
ARGS = ["tunnel", "--no-autoupdate", "--metrics", "0.0.0.0:2000", "run"]


class Executed(Exception):
    pass


@pytest.fixture
def entrypoint(monkeypatch):
    spec = importlib.util.spec_from_file_location("cloudflared_entrypoint", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("TUNNEL_TOKEN_PARAMETER", "TUNNEL_TOKEN_FILE", "AWS_REGION"):
        monkeypatch.delenv(name, raising=False)

    def execv(path, args):
        raise Executed(path, args, os.environ.get("TUNNEL_TOKEN_FILE"))

    monkeypatch.setattr(module.os, "execv", execv)
    return module


@pytest.fixture
def ssm(monkeypatch):
    calls = {"clients": [], "gets": []}
    answer = {"value": TOKEN + "\n", "error": None}

    class FakeSSM:
        def get_parameter(self, **kwargs):
            calls["gets"].append(kwargs)
            if answer["error"]:
                raise answer["error"]
            return {"Parameter": {"Value": answer["value"]}}

    def client(service, **kwargs):
        calls["clients"].append((service, kwargs))
        return FakeSSM()

    monkeypatch.setattr(boto3, "client", client)
    return calls, answer


def run(entrypoint, monkeypatch, path):
    monkeypatch.setenv("TUNNEL_TOKEN_PARAMETER", PARAMETER)
    monkeypatch.setenv("TUNNEL_TOKEN_FILE", str(path))
    with pytest.raises(Executed) as executed:
        entrypoint.main(ARGS)
    return executed.value.args


def test_without_parameter_runs_cloudflared_as_is(entrypoint, ssm):
    calls, _ = ssm
    with pytest.raises(Executed) as executed:
        entrypoint.main(["version"])
    assert executed.value.args == (
        entrypoint.CLOUDFLARED,
        [entrypoint.CLOUDFLARED, "version"],
        None,
    )
    assert calls["clients"] == []


def test_token_goes_to_a_private_file_then_cloudflared(
    entrypoint, ssm, monkeypatch, tmp_path, capsys
):
    calls, _ = ssm
    monkeypatch.setenv("AWS_REGION", "eu-west-3")
    token_file = tmp_path / "token"
    path, args, exported = run(entrypoint, monkeypatch, token_file)

    assert (path, args) == (entrypoint.CLOUDFLARED, [entrypoint.CLOUDFLARED, *ARGS])
    assert exported == str(token_file)  # cloudflared's --token-file
    assert token_file.read_text() == TOKEN
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert calls["gets"] == [{"Name": PARAMETER, "WithDecryption": True}]
    assert calls["clients"][0][1]["region_name"] == "eu-west-3"
    assert TOKEN not in capsys.readouterr().err
    assert all(TOKEN not in value for value in os.environ.values())


def test_earlier_file_is_replaced_and_made_private(
    entrypoint, ssm, monkeypatch, tmp_path
):
    token_file = tmp_path / "token"
    token_file.write_text("an older and longer token " * 4)
    token_file.chmod(0o644)
    run(entrypoint, monkeypatch, token_file)
    assert token_file.read_text() == TOKEN
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_default_path_and_region(entrypoint, ssm, monkeypatch):
    calls, _ = ssm
    monkeypatch.setenv("TUNNEL_TOKEN_PARAMETER", PARAMETER)
    written = {}
    monkeypatch.setattr(
        entrypoint, "write_token", lambda path, token: written.update({path: token})
    )
    with pytest.raises(Executed) as executed:
        entrypoint.main(ARGS)
    assert written == {"/run/cloudflared/token": TOKEN}
    assert executed.value.args[2] == "/run/cloudflared/token"
    # Without AWS_REGION, boto3 falls back to AWS_DEFAULT_REGION.
    assert calls["clients"][0][1]["region_name"] is None


def fails(entrypoint, monkeypatch, path, capsys):
    monkeypatch.setenv("TUNNEL_TOKEN_PARAMETER", PARAMETER)
    monkeypatch.setenv("TUNNEL_TOKEN_FILE", str(path))
    with pytest.raises(SystemExit) as exited:
        entrypoint.main(ARGS)
    assert exited.value.code == 1
    return capsys.readouterr().err


def test_ssm_error_stops_before_cloudflared(
    entrypoint, ssm, monkeypatch, tmp_path, capsys
):
    _, answer = ssm
    answer["error"] = ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": ""}}, "GetParameter"
    )
    err = fails(entrypoint, monkeypatch, tmp_path / "token", capsys)
    assert f"cannot read SSM parameter {PARAMETER}" in err
    assert "ParameterNotFound" in err
    assert not (tmp_path / "token").exists()


@pytest.mark.parametrize("value", ["", "  \n", "two words"])
def test_value_that_is_not_a_token_is_refused(
    entrypoint, ssm, monkeypatch, tmp_path, capsys, value
):
    _, answer = ssm
    answer["value"] = value
    err = fails(entrypoint, monkeypatch, tmp_path / "token", capsys)
    assert "does not hold a tunnel token" in err
    if value.strip():
        assert value not in err
    assert not (tmp_path / "token").exists()


def test_link_in_the_volume_is_not_followed(
    entrypoint, ssm, monkeypatch, tmp_path, capsys
):
    target = tmp_path / "elsewhere"
    target.write_text("")
    link = tmp_path / "token"
    link.symlink_to(target)
    err = fails(entrypoint, monkeypatch, link, capsys)
    assert "cannot write the token" in err
    assert TOKEN not in err
    assert target.read_text() == ""


def test_missing_volume(entrypoint, ssm, monkeypatch, tmp_path, capsys):
    err = fails(entrypoint, monkeypatch, tmp_path / "missing" / "token", capsys)
    assert "No such file or directory" in err
