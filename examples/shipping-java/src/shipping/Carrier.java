package shipping;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.Map;

/** The carrier's HTTP API. */
final class Carrier {
    private Carrier() {}

    private static final HttpClient HTTP = HttpClient.newHttpClient();

    /** What it costs to ship a parcel. */
    static Rate fetchRate(String base, Parcel p) throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/v1/rates?zip=" + p.zip() + "&grams=" + p.grams())).build();
        HttpResponse<String> res = HTTP.send(req, HttpResponse.BodyHandlers.ofString());
        if (res.statusCode() != 200) throw new IOException("rates answered " + res.statusCode());
        Map<String, Object> m = Json.object(res.body());
        return new Rate((String) m.get("service"), ((Long) m.get("cents")).intValue());
    }

    /** Books the shipment; the carrier charges for every booking. */
    static Booking bookShipment(String base, Parcel p, Rate r) throws IOException, InterruptedException {
        String body = String.format("{\"order\": \"%s\", \"zip\": \"%s\", \"service\": \"%s\"}", p.order(), p.zip(), r.service());
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/v1/shipments"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        HttpResponse<String> res = HTTP.send(req, HttpResponse.BodyHandlers.ofString());
        if (res.statusCode() != 201) throw new IOException("shipments answered " + res.statusCode());
        return new Booking(p.order(), (String) Json.object(res.body()).get("tracking"));
    }
}
