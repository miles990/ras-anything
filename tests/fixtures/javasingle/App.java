import com.sun.net.httpserver.HttpServer;
import java.net.*;
import java.net.http.*;

public class App {
    public static void main(String[] args) throws Exception {
        HttpServer s = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
        s.createContext("/hi", x -> {
            x.sendResponseHeaders(200, 3);
            x.getResponseBody().write("hi!".getBytes());
            x.close();
        });
        s.start();
        System.out.println(hi("http://127.0.0.1:" + s.getAddress().getPort(), args.length) + " " + Nested.twice(21));
        s.stop(0);
    }

    static class Nested {
        static int twice(int n) {
            return 2 * n;
        }
    }

    /* unicode before the function: 𝄞 */
    static String hi(String base, int n) throws Exception {
        return HttpClient.newHttpClient().send(HttpRequest.newBuilder(URI.create(base + "/hi")).build(), HttpResponse.BodyHandlers.ofString()).body() + n;
    }
}
