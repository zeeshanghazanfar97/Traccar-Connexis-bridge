#!/bin/sh
# Build a complete MLD car graph in a fresh directory on the Docker host.
set -eu
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "Usage: $0 DATA_DIRECTORY [INPUT.osm.pbf]" >&2
    exit 2
fi

data_dir=$1
input=${2:-}
image=${OSRM_IMAGE:-ghcr.io/project-osrm/osrm-backend:v26.4.1-debian}
threads=${OSRM_THREADS:-2}
mkdir -p -- "$data_dir"
data_dir=$(cd -- "$data_dir" && pwd)
work=$data_dir/.preparing
if ! mkdir -- "$work"; then
    echo "A graph or preparation already exists. Use a fresh data directory." >&2
    exit 1
fi
trap 'rm -rf -- "$work"' EXIT
trap 'exit 1' HUP INT TERM
if [ -e "$data_dir/graph" ]; then
    echo "A graph already exists. Use a fresh data directory." >&2
    exit 1
fi
staging=$work/graph
mkdir -- "$staging"

if [ -n "$input" ]; then
    cp -- "$input" "$staging/roads.osm.pbf"
else
    curl --fail --location --retry 3 \
        --output "$staging/roads.osm.pbf" https://download.geofabrik.de/asia/pakistan-latest.osm.pbf
fi

run_osrm() {
    docker run --rm --network none --user "$(id -u):$(id -g)" \
        -v "$staging:/data" "$image" "$@"
}
run_osrm osrm-extract --threads "$threads" -p /opt/car.lua /data/roads.osm.pbf
run_osrm osrm-partition --threads "$threads" /data/roads.osrm
run_osrm osrm-customize --threads "$threads" /data/roads.osrm
run_osrm osrm-routed --algorithm mld --trial=1 /data/roads.osrm
rm -- "$staging/roads.osm.pbf"
mv -- "$staging" "$data_dir/graph"
echo "Prepared OSRM graph in $data_dir/graph with $image"
