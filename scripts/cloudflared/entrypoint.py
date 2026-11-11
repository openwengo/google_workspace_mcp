"""Entrypoint of the cloudflared image (Dockerfile.cloudflared).

Reads the tunnel token from an SSM SecureString with the container's AWS credentials
(IRSA), writes it to a file only this user can read, then replaces itself with
cloudflared, run with this script's arguments: cloudflared becomes the container's
main process and gets the stop signals.

Environment:
    TUNNEL_TOKEN_PARAMETER  name of the SSM parameter. Unset, cloudflared runs as is
                            (e.g. `docker run IMAGE version`).
    TUNNEL_TOKEN_FILE       where to write the token, default /run/cloudflared/token
                            (mount an in-memory volume there). Exported to cloudflared,
                            which reads it as its --token-file.

The token never goes to the environment, the logs or a Kubernetes secret. It is read
at each container start: after a rotation, restart the pods.
"""

import os
import sys

CLOUDFLARED = "/usr/local/bin/cloudflared"
DEFAULT_TOKEN_FILE = "/run/cloudflared/token"


class TokenError(Exception):
    """The token can't be read or written. The message never contains the token."""


def read_token(parameter: str) -> str:
    # Imported here: without a parameter, cloudflared starts without loading boto3.
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        ssm = boto3.client(
            "ssm",
            # boto3 reads AWS_DEFAULT_REGION only; IRSA sets both.
            region_name=os.environ.get("AWS_REGION") or None,
            config=Config(
                connect_timeout=5,
                read_timeout=10,
                retries={"mode": "standard", "max_attempts": 5},
            ),
        )
        response = ssm.get_parameter(Name=parameter, WithDecryption=True)
    except (BotoCoreError, ClientError) as error:
        raise TokenError(f"cannot read SSM parameter {parameter}: {error}") from None
    token = response["Parameter"]["Value"].strip()
    if not token or any(c.isspace() for c in token):
        raise TokenError(f"SSM parameter {parameter} does not hold a tunnel token")
    return token


def write_token(path: str, token: str) -> None:
    try:
        # O_NOFOLLOW: never write through a link planted in the volume.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as file:
            # The file may come from an earlier start of the container, in the same
            # pod volume, with other permissions.
            os.fchmod(fd, 0o600)
            file.write(token)
    except OSError as error:
        raise TokenError(
            f"cannot write the token to {path}: {error.strerror}"
        ) from None


def main(args: list[str]) -> None:
    parameter = os.environ.get("TUNNEL_TOKEN_PARAMETER", "").strip()
    if parameter:
        path = os.environ.get("TUNNEL_TOKEN_FILE") or DEFAULT_TOKEN_FILE
        try:
            write_token(path, read_token(parameter))
        except TokenError as error:
            print(f"entrypoint: {error}", file=sys.stderr, flush=True)
            sys.exit(1)
        os.environ["TUNNEL_TOKEN_FILE"] = path
        print(
            f"entrypoint: tunnel token read from SSM parameter {parameter}, "
            f"cloudflared reads it from {path}",
            file=sys.stderr,
            flush=True,
        )
    os.execv(CLOUDFLARED, [CLOUDFLARED, *args])


if __name__ == "__main__":
    main(sys.argv[1:])
