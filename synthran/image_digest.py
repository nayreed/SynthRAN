"""Resolve public OCI image tags to immutable digest references."""
from __future__ import annotations

import argparse
import json
import re
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$"
)
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_AUTH_PARAM_RE = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)="([^"]*)"')
_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
_USER_AGENT = "SynthRAN-image-resolver/2"
_DOCKER_REGISTRY = "registry-1.docker.io"
_SUPPORTED_REGISTRIES = {
    "docker.io": _DOCKER_REGISTRY,
    _DOCKER_REGISTRY: _DOCKER_REGISTRY,
    "ghcr.io": "ghcr.io",
}
_SUPPORTED_AUTH_HOSTS = {"auth.docker.io", "ghcr.io"}


def _normalize_public_repository(repository: str) -> tuple[str, str, str]:
    """Return display repository, registry host and canonical repository path."""

    value = repository.strip()
    if not value or "://" in value or "@" in value:
        raise ValueError(f"unsupported public image repository: {repository!r}")

    first, separator, remainder = value.partition("/")
    if first in _SUPPORTED_REGISTRIES:
        if not separator or not remainder:
            raise ValueError(f"missing repository path: {repository!r}")
        registry = _SUPPORTED_REGISTRIES[first]
        path = remainder
        display = path if registry == _DOCKER_REGISTRY else value
    else:
        if "." in first or ":" in first or first == "localhost":
            raise ValueError(f"unsupported public image registry: {first!r}")
        registry = _DOCKER_REGISTRY
        path = value
        display = value

    if not _REPOSITORY_RE.fullmatch(path):
        raise ValueError(f"unsupported public image repository: {repository!r}")

    canonical = path
    if registry == _DOCKER_REGISTRY and "/" not in canonical:
        canonical = f"library/{canonical}"
    return display, registry, canonical


def _normalize_docker_hub_repository(repository: str) -> tuple[str, str]:
    """Compatibility helper retained for the existing Docker Hub caller."""

    display, registry, canonical = _normalize_public_repository(repository)
    if registry != _DOCKER_REGISTRY:
        raise ValueError(f"unsupported Docker Hub repository: {repository!r}")
    return display, canonical


def _token_url(challenge: str, canonical_repository: str) -> str:
    if not challenge or not challenge.lower().startswith("bearer "):
        raise RuntimeError("registry did not return a supported Bearer challenge")
    params = dict(_AUTH_PARAM_RE.findall(challenge[len("Bearer ") :]))
    realm = params.get("realm", "")
    parts = urlsplit(realm)
    if parts.scheme != "https" or parts.hostname not in _SUPPORTED_AUTH_HOSTS:
        raise RuntimeError(f"registry returned an unsupported token realm: {realm!r}")

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if params.get("service"):
        query["service"] = params["service"]
    query["scope"] = params.get("scope") or f"repository:{canonical_repository}:pull"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _anonymous_bearer_token(
    challenge: str,
    canonical_repository: str,
    timeout: float,
) -> str:
    request = Request(
        _token_url(challenge, canonical_repository),
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
    )
    with urlopen(request, timeout=timeout) as response:  # nosec B310: validated HTTPS realm
        payload = json.load(response)
    token = payload.get("token") or payload.get("access_token")
    if not token:
        raise RuntimeError("registry token service did not return a pull token")
    return str(token)


def _manifest_digest(
    registry: str,
    canonical_repository: str,
    tag: str,
    timeout: float,
) -> str:
    url = f"https://{registry}/v2/{canonical_repository}/manifests/{tag}"
    headers = {"Accept": _ACCEPT, "User-Agent": _USER_AGENT}

    def request(extra_headers: dict[str, str] | None = None):
        merged = {**headers, **(extra_headers or {})}
        return Request(url, method="HEAD", headers=merged)

    try:
        response = urlopen(request(), timeout=timeout)  # nosec B310: allowlisted registry host
    except HTTPError as error:
        if error.code != 401:
            raise
        challenge = error.headers.get("WWW-Authenticate") or ""
        token = _anonymous_bearer_token(challenge, canonical_repository, timeout)
        response = urlopen(  # nosec B310: allowlisted registry host
            request({"Authorization": f"Bearer {token}"}),
            timeout=timeout,
        )

    with response:
        digest = (response.headers.get("Docker-Content-Digest") or "").strip().lower()
    if not _DIGEST_RE.fullmatch(digest):
        raise RuntimeError(f"registry returned an invalid manifest digest: {digest!r}")
    return digest


def resolve_public_image(
    reference_repository: str,
    tag: str,
    timeout: float = 20.0,
) -> str:
    display_repository, registry, canonical_repository = _normalize_public_repository(
        reference_repository
    )
    tag = tag.strip()
    if not _TAG_RE.fullmatch(tag):
        raise ValueError(f"invalid OCI image tag: {tag!r}")
    digest = _manifest_digest(registry, canonical_repository, tag, timeout)
    return f"{display_repository}@{digest}"


def resolve_docker_hub(
    reference_repository: str,
    tag: str,
    timeout: float = 20.0,
) -> str:
    """Resolve a Docker Hub tag while preserving the historical public API."""

    _normalize_docker_hub_repository(reference_repository)
    return resolve_public_image(reference_repository, tag, timeout)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    print(resolve_public_image(args.repository, args.tag, args.timeout))


if __name__ == "__main__":
    main()
