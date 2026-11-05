# Traccar + Connexis bridge

This project polls your Connexis tracker API, translates its latest GPS positions into Traccar's OsmAnd HTTP format, and stores history in PostgreSQL/TimescaleDB.

```text
Connexis API → Python adapter → Traccar :5055 → TimescaleDB
                                  ↓
                       Dashboard :8082 (internal)
                                  ↓
                    Traefik → https://traccar.zeeit.dev
```

The implementation follows the API contract in your attached guide. The live Connexis API has not been verified with your credentials. The sample responses in `examples/` are illustrative, not captured account data.

## Files

| File | Purpose |
| --- | --- |
| `compose.yml` | Production stack. Pulls all three images, persists data in host folders, and routes the dashboard through Traefik without publishing ports. Can be pasted into Portainer. |
| `compose.dev.yml` | Standalone development stack with named volumes and a localhost dashboard; builds the adapter as `traccar-connexis-adapter:dev`. |
| `Dockerfile` | Standard-library-only Python 3.12 adapter, running as a non-root user. |
| `.env.example` | API credentials, Docker Hub namespace, release tag, and runtime options. |
| `scripts/build-and-push.sh` | Builds and publishes the adapter image referenced by production Compose. |
| `scripts/prepare-production.sh` | Creates production data folders and sets ownership for the non-root adapter. Run on the Docker host. |
| `adapter/` | API client, data mapping, persistent delivery state, polling, and health check. |
| `tests/` | Unit and mock HTTP integration tests. |

## 1. Configure

Install Docker with modern Docker Compose v2+ and Buildx. The publish script also needs Python 3.9+ on your workstation. The adapter itself needs no pip packages.

```bash
cp .env.example .env
openssl rand -hex 32
```

Edit `.env` and fill in:

```dotenv
DB_PASSWORD=your-generated-password
CONNEXIS_BASE_URL=https://your-provider-host
CONNEXIS_API_TOKEN='the exact Authorization header value'
DOCKERHUB_NAMESPACE=your-dockerhub-username-or-organization
ADAPTER_IMAGE_NAME=traccar-connexis-adapter
ADAPTER_IMAGE_TAG=1.0.0
```

The resulting production image is:

```text
docker.io/<DOCKERHUB_NAMESPACE>/<ADAPTER_IMAGE_NAME>:<ADAPTER_IMAGE_TAG>
```

`DOCKER_REGISTRY` defaults to `docker.io`. You can change it for another registry; `DOCKERHUB_NAMESPACE` then means the namespace at that registry. Docker login credentials stay in Docker's credential store, not in `.env` or the image.

Use the exact Connexis authorization value; the adapter does not prepend `Bearer`. Single-quote tokens containing `$` or `#`. Do not commit `.env`. Compose uses it for interpolation, and passes only the declared environment values to each container.

Confirm `GPS_UTC_OFFSET` and `SPEED_UNIT` with Connexis. The guide assumes UTC+05:00 and km/h. Explicit timezone offsets in API timestamps take precedence over `GPS_UTC_OFFSET`.

## 2. Develop locally

```bash
docker compose -p traccar-dev -f compose.dev.yml up -d --build
docker compose -p traccar-dev -f compose.dev.yml logs -f connexis-adapter
```

Open [http://localhost:8082](http://localhost:8082), create the first administrator account, then add a device whose **Identifier** exactly matches `plateNo`, for example `APL-04`. Repeat for each vehicle. Discovery in the adapter does not create Traccar devices. Until a device exists, its submissions will fail and be retried.

Useful development commands:

```bash
# Discover identifiers from the provider.
docker compose -p traccar-dev -f compose.dev.yml run --rm --no-deps connexis-adapter --list-assets

# Fetch and display unsent mapped positions without forwarding them.
docker compose -p traccar-dev -f compose.dev.yml run --rm --no-deps connexis-adapter --dry-run

# Rebuild after editing the adapter.
docker compose -p traccar-dev -f compose.dev.yml up -d --build connexis-adapter

# Stop while preserving history and adapter state.
docker compose -p traccar-dev -f compose.dev.yml down
```

Use the same `-p traccar-dev` consistently to reuse your development volumes. Development needs no Traefik network or production host folders. `TRACCAR_BIND_ADDRESS` and `TRACCAR_WEB_PORT` apply only to development; production does not publish ports.

## 3. Build and push to Docker Hub

Create the adapter repository in your Docker Hub account or organization, choose its visibility, and log in with an account that can push to it:

```bash
docker login --username YOUR_DOCKERHUB_USERNAME

# Preview the resolved image and command. Does not build or push.
./scripts/build-and-push.sh --dry-run

# Build and publish the release configured in .env.
./scripts/build-and-push.sh
```

The script reads the resolved `compose.yml` model, so the published tag and the production tag match. It uses a named Buildx builder without changing your selected builder. By default it publishes both `linux/amd64` and `linux/arm64`; no API credentials are sent as build arguments or copied into the image. `.dockerignore` includes only the adapter source and Dockerfile.

Optional flags:

```bash
./scripts/build-and-push.sh --env-file .env.production
./scripts/build-and-push.sh --platforms linux/amd64
./scripts/build-and-push.sh --platforms linux/arm64 --load
```

`--load` builds one architecture into your local image store without pushing. Other builds push by default. `BUILD_PLATFORMS` in `.env` controls the default architectures. Set a new `ADAPTER_IMAGE_TAG` for each release so rollback remains straightforward.

## 4. Deploy production

Copy `compose.yml`, a configured `.env`, and `scripts/prepare-production.sh` to your production host, retaining the script's path. The adapter code and Dockerfile are not needed there. Set the same image namespace, name, and tag that you published, plus the production database password and Connexis credentials.

Production matches your existing Traefik setup:

| Setting | Value |
| --- | --- |
| Dashboard hostname | `traccar.zeeit.dev`, configurable with `TRACCAR_DOMAIN` |
| External Docker network | `traefik-public` |
| Entrypoint | `websecure` |
| TLS certificate resolver | `cloudflare` |
| Authentication middleware | `authentik@file` |
| Internal dashboard destination | Traccar port `8082` over HTTP |

Traefik must already be attached to `traefik-public`, with this entrypoint, resolver, and middleware configured. Point the dashboard hostname at your existing Traefik endpoint and configure Authentik for that hostname. Traccar also receives its public HTTPS URL through `WEB_URL`. Authentik protects access to the dashboard; you still create and use a Traccar account.

No service publishes host ports. Only Traccar joins `traefik-public`; the database and adapter communicate with it on the stack's default network. The adapter calls the internal GPS receiver directly, so its submissions do not pass through Authentik.

All persistent production data is bind-mounted beneath `TRACCAR_VOLUME_ROOT`, defaulting to `/home/zeeshan/containers/volumes/traccar`:

| Subfolder | Container path | Data |
| --- | --- | --- |
| `database` | `/var/lib/postgresql/data` | PostgreSQL/TimescaleDB database |
| `logs` | `/opt/traccar/logs` | Traccar logs |
| `data` | `/opt/traccar/data` | Traccar data files |
| `adapter-state` | `/data` | Delivery timestamps and shared API request quota |

Prepare these folders on the Docker host before deployment. The adapter runs as UID/GID `10001:10001`; its state folder must be writable by that user. Bind mounts require existing paths to prevent Docker from silently creating a root-owned adapter folder. Temporary adapter files remain in an in-memory `/tmp` mount.

```bash
# Run on the production Docker host.
sudo ./scripts/prepare-production.sh
# If TRACCAR_VOLUME_ROOT is customized, pass the identical absolute path instead:
# sudo ./scripts/prepare-production.sh /your/custom/traccar

# Required on this host if the adapter repository is private.
docker login --username YOUR_DOCKERHUB_USERNAME

docker network inspect traefik-public
docker compose -p traccar -f compose.yml config --quiet
docker compose -p traccar -f compose.yml pull
docker compose -p traccar -f compose.yml up -d database traccar
```

Open [https://traccar.zeeit.dev](https://traccar.zeeit.dev) (or your configured hostname), authenticate through Authentik, create the first Traccar administrator account, and register your vehicles with their exact plate identifiers. Then start the adapter:

```bash
docker compose -p traccar -f compose.yml up -d connexis-adapter
docker compose -p traccar -f compose.yml ps
docker compose -p traccar -f compose.yml logs -f connexis-adapter
```

Production services use `pull_policy: always`. Deploying requires access to the configured registries. To update only the adapter, publish a new tag, update production `.env`, and run:

```bash
docker compose -p traccar -f compose.yml up -d --no-deps connexis-adapter
```

### Portainer

Use a **Docker Standalone** Stack. Paste the complete `compose.yml` into its Web editor and supply the `.env.example` variables in Portainer's environment variable editor, replacing all placeholders. Portainer does not automatically read your workstation's `.env`.

Publish the adapter first. Run the folder preparation script on the Docker host managed by Portainer, and verify that host has the existing `traefik-public` network. For a private image, configure Docker Hub registry credentials on the Portainer environment. Then deploy the stack, open Traccar through its HTTPS hostname, and register each device. The adapter code is in its image; only persistent data folders are mounted from the host. This configuration is for Compose/Standalone, not Docker Swarm.

## API contract and mapping

The adapter sends `Authorization: <CONNEXIS_API_TOKEN>` on provider requests only:

1. `GET /api/v1/assets/` → `{"status":200,"data":["APL-04"]}`. Objects containing `plateNo` are also accepted.
2. `POST /api/v1/current_location` with JSON `{"plateNo":["APL-04"]}` → `{"status":200,"data":[...positions...]}`.
3. Form-encoded `POST /` to Traccar's internal OsmAnd listener at `http://traccar:5055`.

The two API paths are configurable. If `CONNEXIS_PLATES=APL-04,OTHER-PLATE` is set, discovery is skipped and only those vehicles are forwarded. Pagination and other response wrappers are not assumed.

| Connexis field | Traccar field |
| --- | --- |
| `plateNo` | Device identifier `id` |
| `latitude`, `longitude` | `lat`, `lon`, validated geographic ranges |
| `gpsTime` | `timestamp`, Unix milliseconds |
| `speed` | `speed`, converted from `SPEED_UNIT` to knots |
| `IGNITION` IO | Boolean `ignition`; accepts 1/0, true/false, on/off |
| `MAIN POWER` IO | Numeric `power`, passed through; provider is assumed to report volts |
| `GSM SIGNAL` IO | Numeric custom attribute `gsmSignal` |

Other IO fields are ignored. Missing speed defaults to zero. The guide supplies no GPS-validity field; structurally valid coordinates are sent with `valid=true`. Check the provider contract before adding validity or voltage scaling rules.

## Runtime behavior and limits

- `CONNEXIS_REQUESTS_PER_MINUTE=3` is the default API limit. Set another positive integer in `.env` to match your provider's allowance; both Compose configurations pass it to the adapter. Requests are limited across all Connexis endpoints in a rolling 60-second window, and failed attempts count too. Traccar submissions do not consume this quota.
- The request quota is stored with adapter state, so restarts and manual `--list-assets`, `--dry-run`, and `--once` commands using that same storage share the limit. Separate deployments or other applications using the same API account do not share this state; account for their traffic when choosing the limit.
- `POLL_INTERVAL` remains the minimum pause between completed polling cycles. Rate-limit waits can make cycles take longer. Discovery plus current locations uses two API requests per cycle; set `CONNEXIS_PLATES` to skip discovery and use one. Shutdown interrupts a rate-limit wait promptly.
- Duplicate and older timestamps are skipped per vehicle. SQLite state in the production `adapter-state` folder (development `adapter_state` volume) survives container restarts and updates. Delivery state advances only after an HTTP 200 acknowledgment from Traccar.
- Temporary failures retry with exponential backoff, capped by `MAX_BACKOFF`. A malformed vehicle does not prevent valid vehicles in the response from being processed. No provider response bodies or tokens are logged.
- TLS verification is enabled. `VERIFY_TLS=false` affects Connexis only. For a private CA, mount the certificate read-only and set the adapter's `CONNEXIS_CA_FILE` to its container path through an additional Compose override.
- Health checks measure the last successful polling/forwarding cycle, including successful duplicate-only or empty responses. They do **not** prove the tracker is supplying fresh GPS fixes. An unhealthy status alone does not trigger a Docker restart; retries continue internally.
- Run one adapter instance for this account/state volume. Stop it before using `--once` to send a manual poll; concurrent senders can race. `--dry-run` and `--list-assets` do not forward points.
- A crash after Traccar accepts a point but before the state commit can cause a retry. Server-side duplicate filtering is enabled as an additional safeguard, but delivery is not an exactly-once transaction.
- Traccar may acknowledge before its processing buffer writes a fix to the database. Allow a few seconds before checking history; an acknowledgment alone is not a durable database receipt.
- This integration polls current locations. It cannot recover points that disappear between polls or while the adapter is down, and intentionally skips out-of-order older fixes. Historical backfill needs a documented Connexis history endpoint and a separate import flow.
- Traccar's device online/offline indicator reflects HTTP connections; check the position timestamp for data freshness. Polling more frequently than the provider updates its GPS data does not create extra route detail.

## Persistence and maintenance

Production data lives in the four host folders under `TRACCAR_VOLUME_ROOT`. Keep this path stable when redeploying. Docker Compose teardown does not delete bind-mounted host data. Development uses named volumes: `docker compose down` preserves them, while **`down -v` deletes development data**.

If replacing the earlier production configuration that used named volumes, stop that stack and migrate its database, logs, Traccar data, and adapter state into the matching host folders before starting this configuration. Preserve file ownership during migration and retain the original volumes until the migrated data is verified. Empty host folders initialize a fresh installation.

The database image follows the guide's `timescale/timescaledb:latest-pg17` tag. For controlled upgrades, set `DATABASE_IMAGE` to a tested version or digest before deployment. Do not change the PostgreSQL major version against an existing data volume without a database migration. Changing `DB_PASSWORD` in `.env` does not change the password inside an already initialized database.

Create a database dump before upgrades and store backups outside the Docker host:

```bash
mkdir -p backups
docker compose -p traccar -f compose.yml exec -T database pg_dump -U traccar -d traccar -Fc > backups/traccar.dump
```

For a consistent adapter-state backup, stop the adapter and copy the `adapter-state` folder under `TRACCAR_VOLUME_ROOT`, then start it again. Preserve its ownership and permissions. Restore database history and adapter state from matching backups; restoring old history with newer adapter state can skip fixes.

## Validation and troubleshooting

```bash
python3 -m unittest discover -s tests -v
docker compose --env-file .env.example -f compose.yml config --quiet
docker compose --env-file .env.example -f compose.dev.yml config --quiet
```

The tests include a local mock HTTP server and need permission to bind a loopback port. They do not use real credentials or contact Connexis.

For submission failures, check the exact Traccar device identifier and adapter logs. For provider errors, check its base URL, endpoint paths, authorization format, and TLS certificate. HTTP redirects are deliberately rejected so the authorization header cannot be forwarded elsewhere; configure the final API endpoint URL directly.

A dry run that prints nothing can simply mean the latest fix is already in the state database. Avoid deleting state merely to make logs repeat.

Reference sources: [Traccar Docker deployment](https://www.traccar.org/docker/), [configuration variables](https://www.traccar.org/configuration-file/), [OsmAnd protocol](https://www.traccar.org/osmand/), and [PostgreSQL/TimescaleDB setup](https://www.traccar.org/postgresql/).
