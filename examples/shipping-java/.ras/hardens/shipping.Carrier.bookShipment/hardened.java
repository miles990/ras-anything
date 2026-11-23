// Hardened shipping.Carrier.bookShipment, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`.
package shipping;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import shipping._ras_hardened.RasRt;

// Compiled as a class inside Carrier: its private members and nested types are in scope by their plain names.
final class Hardened {
    // A slow (3 s) answer still fits one attempt, and a hung one leaves room for a second.
    private static final RasRt.Policy SHIPMENTS = RasRt.policy("carrier-shipments")
            .deadline(Duration.ofMillis(4500)).attemptTimeout(Duration.ofMillis(3300)).attempts(4);

    // No RAS_FALLBACK: without the carrier's tracking number there is no booking to report.
    static Booking bookShipment(String base, Parcel p, Rate r) throws IOException, InterruptedException {
        URI uri = URI.create(base + "/v1/shipments");
        String body = String.format("{\"order\": \"%s\", \"zip\": \"%s\", \"service\": \"%s\"}", p.order(), p.zip(), r.service());
        // A lost answer may be a booking already made (and charged): every attempt, and a later rerun, carries this key.
        String key = RasRt.idempotencyKey("shipment", p.order(), p.zip(), r.service());
        try {
            return SHIPMENTS.call("POST", key, a -> {
                HttpResponse<String> res = RasRt.check(send(a, uri, body));
                if (res.statusCode() != 201) throw new IOException("shipments answered " + res.statusCode());
                return booking(p, RasRt.parseJson(res.body())); // strict: a cut-off body is asked for again
            });
        } catch (RasRt.RasError e) { // gave up, already logged; still an IOException to callers
            throw new RasRt.RasIOException(e.getMessage(), e.getCause(), "carrier-shipments");
        } catch (IOException | InterruptedException | RuntimeException e) {
            throw e;
        } catch (Exception e) { // Policy.call declares Exception; nothing here throws another checked one
            throw new IOException(e);
        }
    }

    /** One attempt. The request's timeout ends at the headers, so the wait for the body is bounded as well. */
    private static HttpResponse<String> send(RasRt.Attempt a, URI uri, String body) throws Exception {
        HttpRequest req = a.apply(HttpRequest.newBuilder(uri)) // Idempotency-Key and timeout
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        CompletableFuture<HttpResponse<String>> res = HTTP.sendAsync(req, HttpResponse.BodyHandlers.ofString());
        try {
            return res.get(a.timeout().toNanos(), TimeUnit.NANOSECONDS);
        } catch (ExecutionException e) {
            throw e.getCause() instanceof Exception cause ? cause : e;
        } finally {
            res.cancel(true); // gave up waiting: drop the connection (no effect once answered)
        }
    }

    /** A complete answer without a tracking number is the carrier's bug: an error, not retried. */
    private static Booking booking(Parcel p, Object body) throws IOException {
        if (body instanceof Map<?, ?> m && m.get("tracking") instanceof String tracking && !tracking.isBlank())
            return new Booking(p.order(), tracking);
        throw new RasRt.RasIOException("shipments answered without a tracking number", null, "carrier-shipments");
    }
}
