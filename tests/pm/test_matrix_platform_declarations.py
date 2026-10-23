"""Matrix base dependencies on macOS; preserve Linux crypto and other gates."""
from pathlib import Path
import tomllib

import pytest
from packaging.markers import Marker, default_environment
from packaging.requirements import Requirement


@pytest.mark.parametrize('platform', ['linux', 'darwin', 'win32', 'freebsd'])
def test_matrix_platform_declarations(platform: str) -> None:
    # Platform is marker input data, not a counterfeit interpreter/host.
    root = Path(__file__).resolve().parents[2]
    document = tomllib.loads((root / 'pyproject.toml').read_text())
    environment = default_environment()
    environment['sys_platform'] = platform
    requirements = [Requirement(value) for value in document['project']['optional-dependencies']['matrix']]
    selected = {r.name: r for r in requirements if r.marker is None or r.marker.evaluate(environment)}
    supported = Marker(document['tool']['hermes']['extras-platforms']['matrix']).evaluate(environment)
    if platform not in ('linux', 'darwin'):
        assert not selected
        assert not supported
        return
    assert supported
    assert set(selected) == {'mautrix', 'aiosqlite', 'asyncpg', 'aiohttp-socks', 'aiohttp'}
    assert selected['mautrix'].extras == ({'encryption'} if platform == 'linux' else set())
    for name, version in {'mautrix': '0.21.1', 'aiosqlite': '0.22.1', 'asyncpg': '0.31.0',
                          'aiohttp-socks': '0.11.0', 'aiohttp': '3.14.5'}.items():
        assert str(selected[name].specifier) == f'=={version}'
