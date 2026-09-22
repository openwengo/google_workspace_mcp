"""Keyless outgoing Workspace credentials and shared-drive creation helpers."""

import asyncio
import os
from collections import OrderedDict
from threading import RLock

import boto3
from google.auth import aws, credentials, exceptions
from googleapiclient.discovery import build

from auth.machine_auth import get_machine_token, require_live_machine
from auth.machine_policy import authorize_tool, select_scopes


class IrsaSupplier(aws.AwsSecurityCredentialsSupplier):
    def __init__(self, region):
        self.region = region
        # No fallback to the EKS node's identity. Botocore owns IRSA rotation.
        os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
        self.session = boto3.Session(region_name=region)
        self._lock = RLock()

    def get_aws_region(self, context, request):
        return self.region

    def get_aws_security_credentials(self, context, request):
        # Different target/scope cache entries share this boto3 session.
        with self._lock:
            current = self.session.get_credentials()
            if current is None or current.method != "assume-role-with-web-identity":
                raise exceptions.RefreshError("Expected the MCP pod's IRSA credentials")
            frozen = current.get_frozen_credentials()
        return aws.AwsSecurityCredentials(
            frozen.access_key, frozen.secret_key, frozen.token
        )


class LockedCredentials(credentials.Credentials):
    """Serialize refresh for credentials shared by independent HTTP clients."""

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.lock = RLock()
        self._quota_project_id = source.quota_project_id

    def refresh(self, request):
        with self.lock:
            self.source.refresh(request)
            self.token, self.expiry = self.source.token, self.source.expiry

    def before_request(self, request, method, url, headers):
        with self.lock:
            super().before_request(request, method, url, headers)


class MachineRuntime:
    def __init__(self, policy, wif):
        self.policy, self.wif = policy, wif
        self._cache = OrderedDict()
        self._lock = RLock()
        self._supplier = None

    def credentials(self, target, scopes):
        key = (target.unique_id, tuple(sorted(scopes)))
        # Runtime owns a single immutable policy/provider/project configuration.
        with self._lock:
            if key not in self._cache:
                if self._supplier is None:
                    self._supplier = IrsaSupplier(self.wif.aws_region)
                source = aws.Credentials(
                    audience=self.wif.audience,
                    subject_token_type="urn:ietf:params:aws:token-type:aws4_request",
                    token_url="https://sts.googleapis.com/v1/token",
                    aws_security_credentials_supplier=self._supplier,
                    service_account_impersonation_url=(
                        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                        f"{target.unique_id}:generateAccessToken"
                    ),
                    scopes=scopes,
                    quota_project_id=self.policy.api_consumer_project,
                )
                self._cache[key] = LockedCredentials(source)
                if len(self._cache) > 256:
                    self._cache.popitem(last=False)
            self._cache.move_to_end(key)
            return self._cache[key]


async def get_machine_service(token, service_name, version, tool_name):
    from auth.machine_audit import machine_request_builder

    require_live_machine(token)
    rule = authorize_tool(token.client, tool_name)
    if service_name not in rule.services:
        raise PermissionError("Google service is not authorized for this tool")
    scopes = select_scopes(
        token.client, service_name, service_name in rule.write_services, tool_name
    )
    creds = await asyncio.to_thread(token.runtime.credentials, token.target, scopes)
    service = await asyncio.to_thread(
        build,
        service_name,
        version,
        credentials=creds,
        requestBuilder=machine_request_builder(token),
        cache_discovery=False,
    )
    return service, token.target.email


async def resolve_creation_folder(service, folder_id):
    """Humans retain existing folder handling; machines create in shared drives."""
    from gdrive.drive_helpers import resolve_folder_id

    token = get_machine_token()
    if token is None:
        return await resolve_folder_id(service, folder_id)
    if not folder_id or folder_id == "root":
        folder_id = token.target.creation_folder_id
    if not folder_id:
        raise PermissionError("Configure a shared-drive creation folder or supply one")
    resolved = await resolve_folder_id(service, folder_id)
    metadata = await asyncio.to_thread(
        service.files()
        .get(fileId=resolved, fields="id,driveId,mimeType", supportsAllDrives=True)
        .execute
    )
    if (
        not metadata.get("driveId")
        or metadata.get("mimeType") != "application/vnd.google-apps.folder"
    ):
        raise PermissionError("Machine creation requires a folder in a shared drive")
    return resolved


async def create_machine_file(tool_name, title, mime_type):
    token = get_machine_token()
    service, _ = await get_machine_service(token, "drive", "v3", tool_name)
    try:
        parent = await resolve_creation_folder(service, None)
        result = await asyncio.to_thread(
            service.files()
            .create(
                body={"name": title, "mimeType": mime_type, "parents": [parent]},
                fields="id",
                supportsAllDrives=True,
            )
            .execute
        )
        return result["id"]
    finally:
        service.close()
