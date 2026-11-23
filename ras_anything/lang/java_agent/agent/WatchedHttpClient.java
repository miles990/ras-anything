package _ras_agent;

import java.io.IOException;
import java.net.Authenticator;
import java.net.CookieHandler;
import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpHeaders;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.TreeMap;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executor;
import java.util.concurrent.Flow;
import java.util.concurrent.TimeUnit;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSession;

/** java.net.http.HttpClient with every exchange going through the agent. Bodies are read whole. */
final class WatchedHttpClient extends HttpClient {
    private final HttpClient real;

    WatchedHttpClient(HttpClient real) {
        this.real = real;
    }

    @Override
    public <T> HttpResponse<T> send(HttpRequest request, HttpResponse.BodyHandler<T> handler) throws IOException, InterruptedException {
        Agent.Req r = req(request);
        Agent.Out out;
        if (Agent.verify) {
            Agent.Fate f = Agent.replay(r);
            if (f.hang) Agent.vhang(r.key);
            Agent.vwait(Agent.nanos(f.delay));
            out = f.out;
        } else {
            long t0 = System.nanoTime();
            out = Agent.live(r, () -> real(request));
            Agent.recordIo(r, out, t0);
        }
        if (out.exc != null) throw again(out.exc);
        return answer(request, out, handler);
    }

    @Override
    public <T> CompletableFuture<HttpResponse<T>> sendAsync(HttpRequest request, HttpResponse.BodyHandler<T> handler) {
        Agent.Req r = req(request);
        CompletableFuture<HttpResponse<T>> done = new CompletableFuture<>();
        if (Agent.verify) {
            Agent.Fate f = Agent.replay(r);
            if (!f.hang) Agent.clock.schedule(Agent.clock.now() + Agent.nanos(f.delay), () -> settle(done, request, f.out, handler));
            return done;
        }
        Agent.Attempt a = Agent.liveAttempt(r);
        Agent.Scope s = Agent.current();
        boolean hang = "timeout".equals(a.fault) && r.timeoutS == null;
        Thread t = new Thread(() -> {
            Agent.scope.set(s);
            long t0 = System.nanoTime();
            try {
                if (hang) {
                    if (r.nonidempotent) {
                        try {
                            real(request);
                        } catch (IOException e) {
                            // only the answer is lost
                        }
                        a.reached = true;
                    }
                    hangUntil(done, r);
                    return;
                }
                Agent.Out out = Agent.liveAnswer(r, a, () -> real(request));
                Agent.close(r, a, out);
                if (s != null && "record".equals(Agent.mode)) Agent.recordIo(r, out, t0);
                settle(done, request, out, handler);
            } catch (Throwable e) {
                done.completeExceptionally(e);
            }
        }, "ras-async");
        t.setDaemon(true);
        if (hang) synchronized (Agent.LOCK) {
            Agent.hanging++; // counted before the caller can start to wait
        }
        t.start();
        return done;
    }

    /** An async answer that never comes: the run is a hang unless the app gave up waiting meanwhile. */
    private static void hangUntil(CompletableFuture<?> done, Agent.Req r) throws InterruptedException {
        long end = System.nanoTime() + Agent.nanos(Agent.hangS);
        try {
            while (System.nanoTime() < end) {
                Thread.sleep(5);
                if (done.isDone()) return;
            }
        } finally {
            synchronized (Agent.LOCK) {
                Agent.hanging--;
            }
        }
        if (Agent.gaveUp) return;
        Agent.declareHang(r.key);
    }

    @Override
    public <T> CompletableFuture<HttpResponse<T>> sendAsync(HttpRequest request, HttpResponse.BodyHandler<T> handler, HttpResponse.PushPromiseHandler<T> push) {
        return sendAsync(request, handler);
    }

    private <T> void settle(CompletableFuture<HttpResponse<T>> done, HttpRequest request, Agent.Out out, HttpResponse.BodyHandler<T> handler) {
        try {
            if (out.exc != null) done.completeExceptionally(again(out.exc));
            else done.complete(answer(request, out, handler));
        } catch (IOException e) {
            done.completeExceptionally(e);
        }
    }

    private static IOException again(Exception e) {
        return e instanceof IOException io ? io : new IOException(e);
    }

    private Agent.Out real(HttpRequest request) throws IOException, InterruptedException {
        HttpResponse<byte[]> res = real.send(request, HttpResponse.BodyHandlers.ofByteArray());
        List<List<String>> hs = new ArrayList<>();
        res.headers().map().forEach((k, vs) -> {
            if (!k.startsWith(":") && !k.equalsIgnoreCase("set-cookie")) vs.forEach(v -> hs.add(List.of(k, v)));
        });
        hs.sort((a, b) -> a.get(0).compareTo(b.get(0)));
        return Agent.Out.of(res.statusCode(), res.body(), hs);
    }

    private static Agent.Req req(HttpRequest q) {
        return Agent.Req.http("HttpClient", q.method(), q.uri(), q.headers().firstValue("Idempotency-Key").orElse(null),
                q.bodyPublisher().map(WatchedHttpClient::bytes).orElse(new byte[0]), q.timeout().map(d -> d.toNanos() / 1e9).orElse(null));
    }

    private static byte[] bytes(HttpRequest.BodyPublisher p) {
        java.io.ByteArrayOutputStream b = new java.io.ByteArrayOutputStream();
        CompletableFuture<byte[]> done = new CompletableFuture<>();
        p.subscribe(new Flow.Subscriber<>() {
            @Override
            public void onSubscribe(Flow.Subscription s) {
                s.request(Long.MAX_VALUE);
            }

            @Override
            public void onNext(ByteBuffer x) {
                byte[] part = new byte[x.remaining()];
                x.get(part);
                b.writeBytes(part);
            }

            @Override
            public void onError(Throwable e) {
                done.completeExceptionally(e);
            }

            @Override
            public void onComplete() {
                done.complete(b.toByteArray());
            }
        });
        return done.orTimeout(10, TimeUnit.SECONDS).join();
    }

    private <T> HttpResponse<T> answer(HttpRequest q, Agent.Out out, HttpResponse.BodyHandler<T> handler) throws IOException {
        Map<String, List<String>> m = new TreeMap<>(String.CASE_INSENSITIVE_ORDER);
        for (List<String> h : out.headers) m.computeIfAbsent(h.get(0), k -> new ArrayList<>()).add(h.get(1));
        HttpHeaders headers = HttpHeaders.of(m, (a, b) -> true);
        HttpResponse.BodySubscriber<T> sub = handler.apply(new HttpResponse.ResponseInfo() {
            @Override
            public int statusCode() {
                return out.status;
            }

            @Override
            public HttpHeaders headers() {
                return headers;
            }

            @Override
            public Version version() {
                return Version.HTTP_1_1;
            }
        });
        sub.onSubscribe(new Flow.Subscription() {
            @Override
            public void request(long n) { }

            @Override
            public void cancel() { }
        });
        if (out.body.length > 0) sub.onNext(List.of(ByteBuffer.wrap(out.body)));
        sub.onComplete();
        T body;
        try {
            body = sub.getBody().toCompletableFuture().join();
        } catch (java.util.concurrent.CompletionException e) {
            throw e.getCause() instanceof IOException io ? io : new IOException(e.getCause());
        }
        return new HttpResponse<>() {
            @Override
            public int statusCode() {
                return out.status;
            }

            @Override
            public HttpRequest request() {
                return q;
            }

            @Override
            public Optional<HttpResponse<T>> previousResponse() {
                return Optional.empty();
            }

            @Override
            public HttpHeaders headers() {
                return headers;
            }

            @Override
            public T body() {
                return body;
            }

            @Override
            public Optional<SSLSession> sslSession() {
                return Optional.empty();
            }

            @Override
            public URI uri() {
                return q.uri();
            }

            @Override
            public Version version() {
                return Version.HTTP_1_1;
            }

            @Override
            public String toString() {
                return "(" + q.method() + " " + q.uri() + ") " + out.status;
            }
        };
    }

    @Override
    public Optional<CookieHandler> cookieHandler() {
        return real.cookieHandler();
    }

    @Override
    public Optional<Duration> connectTimeout() {
        return real.connectTimeout();
    }

    @Override
    public Redirect followRedirects() {
        return real.followRedirects();
    }

    @Override
    public Optional<ProxySelector> proxy() {
        return real.proxy();
    }

    @Override
    public SSLContext sslContext() {
        return real.sslContext();
    }

    @Override
    public SSLParameters sslParameters() {
        return real.sslParameters();
    }

    @Override
    public Optional<Authenticator> authenticator() {
        return real.authenticator();
    }

    @Override
    public Version version() {
        return real.version();
    }

    @Override
    public Optional<Executor> executor() {
        return real.executor();
    }

    @Override
    public java.net.http.WebSocket.Builder newWebSocketBuilder() {
        return real.newWebSocketBuilder();
    }
}
