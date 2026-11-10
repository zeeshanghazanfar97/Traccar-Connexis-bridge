// Replay supplies fixes in time order. OSRM accepts at most 100 per request.
export function traceBatches(positions) {
  const batches = [];
  let points = [];
  let partial = false;
  let deviceId;

  const flush = () => {
    if (points.length > 1) batches.push({ deviceId, points });
    points = [];
  };

  for (const position of positions) {
    const { longitude, latitude } = position;
    const timestamp = Math.floor(Date.parse(position.fixTime) / 1000);
    if (!position.valid || !Number.isFinite(longitude) || Math.abs(longitude) > 180
        || !Number.isFinite(latitude) || Math.abs(latitude) > 90
        || !Number.isInteger(timestamp) || timestamp < 0 || timestamp > 0xffffffff) {
      flush();
      partial = true;
      continue;
    }
    const previous = points.at(-1);
    if (previous && (deviceId !== position.deviceId || timestamp < previous.timestamp
        || timestamp - previous.timestamp > 60)) {
      flush();
      partial = true;
    } else if (previous && timestamp === previous.timestamp) {
      partial = true;
      continue;
    }
    if (points.length === 100) {
      flush();
      points.push(previous); // Share one fix so that a batch boundary doesn't lose a leg.
    }
    deviceId = position.deviceId;
    points.push({ longitude, latitude, timestamp });
  }
  flush();
  return { batches, partial };
}

export async function matchTrace(positions, signal) {
  const { batches, partial: hasGaps } = traceBatches(positions);
  const lines = [];
  let partial = hasGaps;
  for (const { deviceId, points } of batches) {
    const response = await fetch(`api/osrm/match?deviceId=${encodeURIComponent(deviceId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(points),
      signal,
    });
    if (!response.ok) throw new Error('Road matching unavailable');
    const result = await response.json();
    lines.push(...result.lines);
    partial ||= result.partial;
  }
  return { lines, status: !lines.length ? 'empty' : partial ? 'partial' : 'ready' };
}
