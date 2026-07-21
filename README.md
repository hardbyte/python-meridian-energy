# meridian-energy

[![PyPI](https://img.shields.io/pypi/v/meridian-energy.svg)](https://pypi.org/project/meridian-energy/)

Python client for the [Meridian Energy](https://www.meridianenergy.co.nz/) (NZ) customer API.

Unofficial. Not affiliated with or endorsed by Meridian Energy.

Talks to the current MyMeridian stack:

1. Email one-time code (`auth.meridianenergy.nz`)
2. Firebase custom-token exchange
3. GraphQL (“Kraken”) at `api.meridianenergy.nz`

## Install

```bash
pip install meridian-energy
# or
uv add meridian-energy
```

Requires Python 3.11+.

## CLI

```bash
uvx meridian login                 # emails a code, prompts for it
uvx meridian accounts
uvx meridian usage --days 14
uvx meridian usage --json
uvx meridian status
uvx meridian logout
```

Tokens are cached at `~/.cache/meridian-energy/tokens.json` (mode `0600`).
Override with `--token-cache` or `MERIDIAN_TOKEN_CACHE`. Email can come from
`MERIDIAN_EMAIL`.

## Library

```python
import asyncio
import httpx
from meridian_energy import MeridianEnergyApi, MeridianEnergyAuth

async def main() -> None:
    auth = MeridianEnergyAuth()
    async with httpx.AsyncClient(timeout=30) as http:
        journey = await auth.request_otp(http, "you@example.com")
        await auth.verify_otp(http, "you@example.com", "123456", journey)

    async with MeridianEnergyApi(auth) as api:
        for account in await api.get_accounts():
            print(account.number, account.primary_icp)
            usage = await api.get_usage(account.number, days=10)
            print(usage.import_kwh, usage.export_kwh, usage.cost_nzd)

asyncio.run(main())
```

Resume a session from stored tokens (refresh is transparent):

```python
from meridian_energy import MeridianEnergyAuth, MeridianEnergyApi, TokenSet

async def save(tokens: TokenSet) -> None:
    store.write(tokens.to_dict())

auth = MeridianEnergyAuth(
    tokens=TokenSet.from_dict(store.read()),
    on_token_update=save,
)
api = MeridianEnergyApi(auth)
```

## Development

```bash
uv sync --group dev
uv run ruff check src tests
uv run ruff format src tests
uv run ty check src
uv run pytest
```

## Home Assistant

The companion integration is [`hardbyte/ha-meridian-energy`](https://github.com/hardbyte/ha-meridian-energy).
