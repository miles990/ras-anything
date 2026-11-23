package shipping;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

/** A local stand-in for the carrier: always healthy, honours Idempotency-Key. */
final class FakeCarrier implements AutoCloseable {
    private final HttpServer server;
    private final Map<String, String> booked = new HashMap<>();

    FakeCarrier() throws IOException {
        server = HttpServer.create(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0), 0);
        server.createContext("/v1/rates", this::rates);
        server.createContext("/v1/shipments", this::shipments);
        server.start();
    }

    String url() {
        return "http://127.0.0.1:" + server.getAddress().getPort();
    }

    private void rates(HttpExchange x) throws IOException {
        String q = x.getRequestURI().getQuery();
        int grams = Integer.parseInt(q.substring(q.indexOf("grams=") + 6));
        String service = grams < 500 ? "letter" : grams < 2000 ? "parcel" : "freight";
        answer(x, 200, "{\"service\": \"" + service + "\", \"cents\": " + (390 + grams / 4) + ", \"currency\": \"EUR\"}");
    }

    private synchronized void shipments(HttpExchange x) throws IOException {
        String body = new String(x.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        String key = x.getRequestHeaders().getFirst("Idempotency-Key");
        String done = key == null ? null : booked.get(key);
        if (done == null) {
            String order = (String) Json.object(body).get("order");
            done = "{\"order\": \"" + order + "\", \"tracking\": \"TRK-" + order.substring(4) + "\", \"status\": \"booked\"}";
            if (key != null) booked.put(key, done);
        }
        answer(x, 201, done);
    }

    private static void answer(HttpExchange x, int status, String body) throws IOException {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        x.getResponseHeaders().set("Content-Type", "application/json");
        x.sendResponseHeaders(status, b.length);
        x.getResponseBody().write(b);
        x.close();
    }

    @Override
    public void close() {
        server.stop(0);
    }
}
