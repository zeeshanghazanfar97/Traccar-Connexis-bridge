# Road matching in Traccar Replay

Open a device's **Replay**, select a time range, and switch on **Road-matched path**. The dashed blue line follows roads that OSRM could match. The speed-colored GPS trace, point details, playback markers and KML download use the original Traccar positions. This works for history already in the database.

The optional Traccar image in `osrm/traccar/` extends version **6.17.0** with a Replay control and an authenticated API resource. Traccar sends time-ordered fixes to the self-hosted OSRM car profile on the Docker network. Matching runs when someone requests the overlay. The Connexis poller and database need no changes.

## Prepare the road graph

On the Docker host, choose a directory where you can write. For local development, use the default data directory.

```sh
./scripts/prepare-osrm.sh ./data/osrm
```

The script downloads Pakistan from Geofabrik, runs OSRM extract, partition and customize, and verifies that the routing server can load the graph before installing it in `graph/`. Allow disk space for the download, intermediate files and the finished graph. Processing uses two threads by default. The input data is from [OpenStreetMap contributors](https://www.openstreetmap.org/copyright). [Pakistan extract](https://download.geofabrik.de/asia/pakistan.html) · [OSRM Docker instructions](https://github.com/Project-OSRM/osrm-backend#using-docker)

To use an existing extract, pass it as the second argument. Choose a **fresh directory** for each rebuild.

```sh
./scripts/prepare-osrm.sh /path/to/new/osrm /path/to/region.osm.pbf
```

The shell script reads `OSRM_IMAGE` and `OSRM_THREADS` from its environment. If you override the image in Compose or Portainer, export the identical value before preprocessing. The default for both is `ghcr.io/project-osrm/osrm-backend:v26.4.1-debian`. Rebuild the graph when changing OSRM versions or profiles. To refresh an active installation, build in a new directory, change `OSRM_DATA_DIR`, and recreate the OSRM service. Keep the previous directory for rollback.

## Enable locally

Build the custom image from the project root.

```sh
docker build -t traccar-osrm:6.17.0-local osrm/traccar
```

In the development project’s configured `.env`, select the image and profile.

```dotenv
TRACCAR_IMAGE=traccar-osrm:6.17.0-local
COMPOSE_PROFILES=osrm
```

Development expects `./data/osrm/graph` by default. If you prepared another location, set `OSRM_DATA_DIR` to its parent directory. Start the matcher and updated dashboard using your existing Compose project name.

```sh
docker compose -p traccar-dev -f compose.dev.yml up -d osrm traccar
```

The standard development command in the README starts the Connexis adapter alongside these services if desired.

## Deploy with Compose or Portainer

1. Build and push a Traccar image to a registry accessible from your Docker host. Replace the image name with your namespace and release tag. The existing adapter publish script builds only the Connexis adapter.

   ```sh
   docker buildx build --platform linux/amd64,linux/arm64 \
     --tag docker.io/YOUR_NAMESPACE/traccar-osrm:6.17.0-1 --push osrm/traccar
   ```

2. Copy `scripts/prepare-osrm.sh` to the Docker host and prepare a graph there. Production expects the `osrm` subdirectory of `TRACCAR_VOLUME_ROOT`. With the default volume root, run `./scripts/prepare-osrm.sh /home/zeeshan/containers/volumes/traccar/osrm`. If you choose another location, set `OSRM_DATA_DIR` explicitly.
3. Use the updated `compose.yml` with your existing stack name and settings. Set `TRACCAR_IMAGE` to the published tag and `COMPOSE_PROFILES=osrm` in the production environment. In Portainer, enter both in the **Docker Standalone** stack's environment editor and redeploy. If your Portainer version skips profiled services, remove the single `profiles: ["osrm"]` line from the `osrm` service in the stack editor and redeploy. Confirm that both `traccar` and `osrm` are running. For a Compose deployment, run the following command.

   ```sh
   docker compose -p traccar -f compose.yml up -d osrm traccar
   ```

OSRM listens internally on port 5000 and mounts the graph read-only. Only the dashboard uses Traefik. For an existing self-hosted OSRM instance, use the custom Traccar image, set `OSRM_URL` to its base URL as reachable **from Traccar**, and leave the Compose profile disabled. This destination receives the GPS fixes selected for matching.

To disable the feature, switch `TRACCAR_IMAGE` back to the original Traccar tag, stop the `osrm` service and remove the profile setting. Redeploy Traccar with the same database and volumes.

## Verify a trip

Choose a known moving trip with several GPS samples less than a minute apart. In the browser's Network panel, enabling the overlay sends same-origin `POST api/osrm/match?deviceId=…` requests. Traccar checks the session, device access and report permissions. Each request carries up to 100 fixes, in longitude/latitude order with Unix timestamps in seconds. The server returns separate lines for split matches. Adjacent batches share one fix.

The overlay breaks at invalid fixes and gaps over 60 seconds. Same-second fixes are coalesced for matching. OSRM can discard outliers, and sparse or off-road sections can have no plausible match. If nothing matches or OSRM is unavailable, Replay shows a status message and the raw trace remains usable. The feature estimates historical paths and supplies no navigation or ETA controls. [OSRM Match API](https://project-osrm.org/docs/v26.4.0/http#match-service)

If the switch is missing after an upgrade, check the running Traccar image tag and reload the web app to pick up its new assets. If the switch reports an unavailable service, check the OSRM container logs, mounted graph path and `OSRM_URL`. An empty match with a healthy server can indicate that the graph covers a different region.
