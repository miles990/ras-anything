package shop;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.util.List;

public class Main {
    static int orders;

    static void answer(HttpExchange x, int status, String body) throws java.io.IOException {
        x.getRequestBody().readAllBytes();
        x.sendResponseHeaders(status, body.length());
        x.getResponseBody().write(body.getBytes());
        x.close();
    }

    public static void main(String[] args) throws Exception {
        HttpServer s = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
        s.createContext("/price", x -> answer(x, 200, "price=" + (100 + x.getRequestURI().getQuery().length()) + ";"));
        s.createContext("/stock", x -> answer(x, 200, "stock=7;"));
        s.createContext("/orders", x -> answer(x, 201, "order=O-" + ++orders + ";"));
        s.createContext("/gone", x -> answer(x, 404, "no"));
        s.start();
        String base = "http://127.0.0.1:" + s.getAddress().getPort();
        Shop.suffix = "-eu";
        for (String sku : List.of("ant", "bee", "centipede")) {
            Price p = Shop.price(base, sku);
            Order o = Shop.order(base, sku, List.of(1, 2));
            System.out.println(p + " " + o + " " + Shop.stamp(sku) + " " + Shop.async(base, sku) + " " + Shop.pooled(base, sku) + " " + new Shop().instance(base));
        }
        System.out.println(Shop.gone(base, args.length > 0 ? args[0] : "x"));
        System.exit(0);
    }
}
