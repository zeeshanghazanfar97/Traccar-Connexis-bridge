import { useEffect, useMemo, useState } from 'react';
import { FormControlLabel, Switch, Typography } from '@mui/material';
import useMapLayer from '../../map/core/useMapLayer';
import { toMapCoordinates } from '../../map/core/mapUtil';
import { matchTrace } from './matchTrace.mjs';

const messages = {
  loading: 'Matching roads…',
  empty: 'No roads matched these fixes.',
  partial: 'Only matched sections are shown in blue.',
  error: 'Road matching is unavailable. Switch it on again to retry.',
};

export const RoadMatchControl = ({ enabled, onChange, status }) => (
  <>
    <FormControlLabel
      control={<Switch checked={enabled} onChange={(event) => onChange(event.target.checked)} />}
      label="Road-matched path"
    />
    {messages[status] && <Typography variant="caption" role="status">{messages[status]}</Typography>}
  </>
);

export function useRoadMatch(positions, enabled) {
  const [result, setResult] = useState(null);
  useEffect(() => {
    if (!enabled) {
      setResult(null);
      return undefined;
    }
    const controller = new AbortController();
    matchTrace(positions, controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setResult({ ...data, positions });
      },
      () => {
        if (!controller.signal.aborted) setResult({ positions, status: 'error' });
      },
    );
    return () => controller.abort();
  }, [positions, enabled]);
  if (!enabled) return {};
  return result?.positions === positions ? result : { status: 'loading' };
}

export function RoadMatchLayer({ lines }) {
  const data = useMemo(() => ({
    type: 'Feature',
    properties: {},
    geometry: {
      type: 'MultiLineString',
      coordinates: lines.map((line) => line.map(([lon, lat]) => toMapCoordinates(lon, lat))),
    },
  }), [lines]);
  useMapLayer({
    source: { attribution: 'Road matching © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>' },
    layers: [{
      type: 'line',
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#1565c0', 'line-width': 4, 'line-dasharray': [2, 1] },
    }],
    layersDeps: [],
    data,
    dataDeps: [data],
  });
  return null;
}
