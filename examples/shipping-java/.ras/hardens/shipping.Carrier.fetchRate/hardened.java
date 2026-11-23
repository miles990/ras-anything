// Hardened shipping.Carrier.fetchRate, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`.
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
    private static final RasRt.Policy RATES = RasRt.policy("carrier-rates")
            .deadline(Duration.ofMillis(4500)).attemptTimeout(Duration.ofMillis(3300)).attempts(4);

    // No RAS_FALLBACK: callers book and bill at this rate, so a made-up or stale one is worse than an error.
    static Rate fetchRate(String base, Parcel p) throws IOException, InterruptedException {
        URI uri = URI.create(base + "/v1/rates?zip=" + p.zip() + "&grams=" + p.grams());
        try {
            return RATES.call(a -> {
                HttpResponse<String> res = RasRt.check(send(a, uri));
                if (res.statusCode() != 200) throw new IOException("rates answered " + res.statusCode());
                return rate(RasRt.parseJson(res.body())); // strict: a cut-off body is retried
            });
        } catch (RasRt.RasError e) { // gave up, already logged; still an IOException to callers
            throw new RasRt.RasIOException(e.getMessage(), e.getCause(), "carrier-rates");
        } catch (IOException | InterruptedException | RuntimeException e) {
            throw e;
        } catch (Exception e) { // Policy.call declares Exception; nothing here throws another checked one
            throw new IOException(e);
        }
    }

    /** One attempt. The request's timeout ends at the headers, so the wait for the body is bounded as well. */
    private static HttpResponse<String> send(RasRt.Attempt a, URI uri) throws Exception {
        CompletableFuture<HttpResponse<String>> res =
                HTTP.sendAsync(a.apply(HttpRequest.newBuilder(uri)).build(), HttpResponse.BodyHandlers.ofString());
        try {
            return res.get(a.timeout().toNanos(), TimeUnit.NANOSECONDS);
        } catch (ExecutionException e) {
            throw e.getCause() instanceof Exception cause ? cause : e;
        } finally {
            res.cancel(true); // gave up waiting: drop the connection (no effect once answered)
        }
    }

    /** A complete answer without a usable rate is the carrier's bug: an error, not retried. */
    private static Rate rate(Object body) throws IOException {
        if (body instanceof Map<?, ?> m && m.get("service") instanceof String service
                && m.get("cents") instanceof Long cents && cents >= 0 && cents <= Integer.MAX_VALUE)
            return new Rate(service, cents.intValue());
        throw new RasRt.RasIOException("rates answered without a usable rate", null, "carrier-rates");
    }
}
