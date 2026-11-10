package org.traccar.api.resource;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.inject.Inject;
import jakarta.ws.rs.BadRequestException;
import jakarta.ws.rs.Consumes;
import jakarta.ws.rs.POST;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.ProcessingException;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.QueryParam;
import jakarta.ws.rs.client.Client;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import org.glassfish.jersey.client.ClientProperties;
import org.traccar.api.BaseResource;
import org.traccar.config.Config;
import org.traccar.model.Device;
import org.traccar.model.UserRestrictions;
import org.traccar.storage.StorageException;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.StringJoiner;

@Path("osrm/match")
@Consumes(MediaType.APPLICATION_JSON)
@Produces(MediaType.APPLICATION_JSON)
public class OsrmResource extends BaseResource {
    public record Fix(Double longitude, Double latitude, Long timestamp) {}

    @Inject private Client client;
    @Inject private Config config;
    @Inject private ObjectMapper mapper;

    @POST
    public Response match(@QueryParam("deviceId") long deviceId, InputStream body) throws StorageException, IOException {
        permissionsService.checkPermission(Device.class, getUserId(), deviceId);
        permissionsService.checkRestriction(getUserId(), UserRestrictions::getDisableReports);

        // Apply a byte limit before deserialization as well as the OSRM point limit.
        byte[] bytes = body.readNBytes(16385);
        if (bytes.length > 16384) {
            return Response.status(413).build();
        }
        List<Fix> fixes;
        try {
            fixes = mapper.readerForListOf(Fix.class).readValue(bytes);
        } catch (IOException e) {
            throw new BadRequestException("Invalid trace");
        }
        if (fixes == null || fixes.size() < 2 || fixes.size() > 100) {
            throw new BadRequestException("Expected 2 to 100 fixes");
        }
        var coordinates = new StringJoiner(";");
        var timestamps = new StringJoiner(";");
        long previous = -1;
        for (Fix fix : fixes) {
            if (fix == null || !inRange(fix.longitude(), 180) || !inRange(fix.latitude(), 90)
                    || fix.timestamp() == null || fix.timestamp() <= previous || fix.timestamp() > 0xffffffffL) {
                throw new BadRequestException("Invalid coordinates or timestamps");
            }
            coordinates.add(String.format(Locale.ROOT, "%.6f,%.6f", fix.longitude(), fix.latitude()));
            timestamps.add(fix.timestamp().toString());
            previous = fix.timestamp();
        }

        String url = config.getString("osrm.url");
        if (url == null) {
            return Response.status(503).build();
        }
        var target = client.target(url).path("match/v1/driving").path(coordinates.toString())
                .queryParam("timestamps", timestamps.toString()).queryParam("gaps", "split")
                .queryParam("overview", "full").queryParam("geometries", "geojson")
                .queryParam("generate_hints", false);
        try (Response upstream = target.property(ClientProperties.FOLLOW_REDIRECTS, false)
                .request(MediaType.APPLICATION_JSON_TYPE).get()) {
            if (!upstream.hasEntity() || (upstream.getStatus() != 200 && upstream.getStatus() != 400)) {
                return Response.status(502).build();
            }
            JsonNode result = upstream.readEntity(JsonNode.class);
            if (result == null || !result.isObject()) {
                return Response.status(502).build();
            }
            String code = result.path("code").asText();
            if (upstream.getStatus() == 400 && ("NoMatch".equals(code) || "NoSegment".equals(code))) {
                return Response.ok(Map.of("lines", List.of(), "partial", true)).build();
            }
            if (upstream.getStatus() != 200 || !"Ok".equals(code)
                    || !result.path("matchings").isArray() || !result.path("tracepoints").isArray()
                    || result.get("tracepoints").size() != fixes.size()) {
                return Response.status(502).build();
            }
            var lines = new ArrayList<JsonNode>();
            for (JsonNode matching : result.get("matchings")) {
                JsonNode geometry = matching.path("geometry");
                JsonNode line = geometry.path("coordinates");
                if (!"LineString".equals(geometry.path("type").asText()) || !line.isArray() || line.size() < 2) {
                    return Response.status(502).build();
                }
                for (JsonNode point : line) {
                    if (!point.isArray() || point.size() != 2
                            || !point.get(0).isNumber() || !inRange(point.get(0).doubleValue(), 180)
                            || !point.get(1).isNumber() || !inRange(point.get(1).doubleValue(), 90)) {
                        return Response.status(502).build();
                    }
                }
                lines.add(line);
            }
            boolean partial = lines.size() != 1 || result.get("tracepoints").valueStream().anyMatch(JsonNode::isNull);
            return Response.ok(Map.of("lines", lines, "partial", partial)).build();
        } catch (ProcessingException e) {
            return Response.status(502).build();
        }
    }

    private static boolean inRange(Double value, int limit) {
        return value != null && Math.abs(value) <= limit;
    }
}
