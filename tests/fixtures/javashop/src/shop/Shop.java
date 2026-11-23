package shop;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse.BodyHandlers;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

public class Shop {
    static String suffix = "";
    private static final HttpClient HTTP = HttpClient.newBuilder().build();

    static String field(String body, String name) throws IOException {
        if (!body.startsWith(name + "=") || !body.endsWith(";")) throw new IOException("bad body");
        return body.substring(name.length() + 1, body.length() - 1);
    }

    static Price price(String base, String sku) throws IOException {
        HttpURLConnection c = (HttpURLConnection) URI.create(base + "/price?sku=" + sku).toURL().openConnection();
        return new Price(sku + suffix, Integer.parseInt(field(new String(c.getInputStream().readAllBytes()), "price")));
    }

    public static Order order(String base, String sku, List<Integer> lots) throws Exception {
        String stock = field(HTTP.send(HttpRequest.newBuilder(URI.create(base + "/stock?sku=" + sku)).build(), BodyHandlers.ofString()).body(), "stock");
        var res = HTTP.send(HttpRequest.newBuilder(URI.create(base + "/orders"))
                .POST(HttpRequest.BodyPublishers.ofString(sku + ":" + Integer.parseInt(stock))).build(), BodyHandlers.ofString());
        if (res.statusCode() != 201) throw new IOException("orders " + res.statusCode());
        return new Order(field(res.body(), "order"), lots, 0.5);
    }

    static String stamp(String id) throws Exception {
        Process p = Runtime.getRuntime().exec(new String[] {"echo", "stamp=" + id + ";"});
        if (!p.waitFor(2, TimeUnit.SECONDS)) {
            p.destroyForcibly();
            throw new IOException("echo timed out");
        }
        if (p.exitValue() != 0) throw new IOException("echo " + p.exitValue());
        return field(new String(p.getInputStream().readAllBytes()).trim(), "stamp");
    }

    static String async(String base, String sku) throws Exception {
        return HTTP.sendAsync(HttpRequest.newBuilder(URI.create(base + "/stock?async=" + sku)).build(), BodyHandlers.ofString())
                .thenApply(r -> r.statusCode() + r.body()).get(2, TimeUnit.SECONDS);
    }

    static String pooled(String base, String sku) throws Exception {
        ExecutorService ex = Executors.newSingleThreadExecutor();
        try {
            return ex.submit(() -> HTTP.send(HttpRequest.newBuilder(URI.create(base + "/stock?pool=" + sku)).build(), BodyHandlers.ofString()).body()).get(1, TimeUnit.SECONDS);
        } finally {
            ex.shutdownNow();
        }
    }

    static String gone(String base, String id) {
        try {
            return new String(URI.create(base + "/gone?" + id).toURL().openStream().readAllBytes());
        } catch (IOException e) {
            return "gone";
        }
    }

    String instance(String base) throws Exception {
        return HTTP.send(HttpRequest.newBuilder(URI.create(base + "/stock")).build(), BodyHandlers.ofString()).body();
    }
}
