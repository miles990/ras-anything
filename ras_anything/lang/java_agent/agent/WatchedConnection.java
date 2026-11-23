package _ras_agent;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.FileNotFoundException;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.ProtocolException;
import java.net.URI;
import java.net.URL;
import java.net.URLConnection;
import java.security.Principal;
import java.security.cert.Certificate;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import javax.net.ssl.HttpsURLConnection;

/** HttpURLConnection (and https) with the exchange going through the agent. Bodies are read whole. */
final class WatchedConnection {
    private final HttpURLConnection real, self;
    private final ByteArrayOutputStream body = new ByteArrayOutputStream();
    private Agent.Out out;
    private IOException failed;

    private WatchedConnection(HttpURLConnection real, HttpURLConnection self) {
        this.real = real;
        this.self = self;
    }

    static URLConnection of(URL u, URLConnection c) {
        if (!Agent.watching() || !(c instanceof HttpURLConnection h)) return c;
        return h instanceof HttpsURLConnection s ? new Https(u, s) : new Http(u, h);
    }

    private Agent.Out exchange() throws IOException {
        if (failed != null) throw failed;
        if (out != null) return out;
        String method = self.getDoOutput() && self.getRequestMethod().equals("GET") ? "POST" : self.getRequestMethod();
        byte[] data = body.toByteArray();
        Agent.Req r = Agent.Req.http("HttpURLConnection", method, URI.create(self.getURL().toString()), self.getRequestProperty("Idempotency-Key"),
                data, self.getReadTimeout() > 0 ? self.getReadTimeout() / 1000.0 : null);
        try {
            if (Agent.verify) {
                Agent.Fate f = Agent.replay(r);
                if (f.hang) Agent.vhang(r.key);
                Agent.vwait(Agent.nanos(f.delay));
                out = f.out;
            } else {
                long t0 = System.nanoTime();
                out = Agent.live(r, () -> real(method, data));
                Agent.recordIo(r, out, t0);
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new InterruptedIOException();
        }
        if (out.exc != null) throw failed = out.exc instanceof IOException io ? io : new IOException(out.exc);
        return out;
    }

    private Agent.Out real(String method, byte[] data) throws IOException {
        if (data.length > 0 || self.getDoOutput()) {
            real.setDoOutput(true);
            real.setRequestMethod(method);
            try (OutputStream o = real.getOutputStream()) {
                o.write(data);
            }
        }
        int status = real.getResponseCode();
        InputStream in = status >= 400 ? real.getErrorStream() : real.getInputStream();
        byte[] b = in == null ? new byte[0] : in.readAllBytes();
        List<List<String>> hs = new ArrayList<>();
        real.getHeaderFields().forEach((k, vs) -> {
            if (k != null && !k.equalsIgnoreCase("set-cookie")) vs.forEach(v -> hs.add(List.of(k, v)));
        });
        hs.sort((x, y) -> x.get(0).compareTo(y.get(0)));
        return Agent.Out.of(status, b, hs);
    }

    private InputStream input() throws IOException {
        Agent.Out o = exchange();
        if (o.status == 404 || o.status == 410) throw new FileNotFoundException(self.getURL().toString());
        if (o.status >= 400) throw new IOException("Server returned HTTP response code: " + o.status + " for URL: " + self.getURL());
        return new ByteArrayInputStream(o.body);
    }

    private InputStream error() {
        return out != null && out.exc == null && out.status >= 400 && out.body.length > 0 ? new ByteArrayInputStream(out.body) : null;
    }

    private OutputStream output() throws IOException {
        if (!self.getDoOutput()) throw new ProtocolException("cannot write to a URLConnection if doOutput=false - call setDoOutput(true)");
        return body;
    }

    private int code() throws IOException {
        return exchange().status;
    }

    private Map<String, List<String>> headers() {
        Map<String, List<String>> m = new LinkedHashMap<>();
        try {
            m.put(null, List.of("HTTP/1.1 " + exchange().status));
            for (List<String> h : exchange().headers) m.computeIfAbsent(h.get(0), k -> new ArrayList<>()).add(h.get(1));
        } catch (IOException e) {
            return Map.of();
        }
        return m;
    }

    private String header(String name) {
        String v = null;
        for (Map.Entry<String, List<String>> e : headers().entrySet())
            if (name == null ? e.getKey() == null : name.equalsIgnoreCase(e.getKey())) v = e.getValue().get(e.getValue().size() - 1);
        return v;
    }

    private static final class Http extends HttpURLConnection {
        private final HttpURLConnection real;
        private final WatchedConnection w;

        Http(URL u, HttpURLConnection real) {
            super(u);
            this.real = real;
            w = new WatchedConnection(real, this);
        }

        @Override
        public void setRequestMethod(String m) throws ProtocolException {
            super.setRequestMethod(m);
            real.setRequestMethod(m);
        }

        @Override
        public void setRequestProperty(String k, String v) {
            super.setRequestProperty(k, v);
            real.setRequestProperty(k, v);
        }

        @Override
        public void addRequestProperty(String k, String v) {
            super.addRequestProperty(k, v);
            real.addRequestProperty(k, v);
        }

        @Override
        public void setConnectTimeout(int ms) {
            super.setConnectTimeout(ms);
            real.setConnectTimeout(ms);
        }

        @Override
        public void setReadTimeout(int ms) {
            super.setReadTimeout(ms);
            real.setReadTimeout(ms);
        }

        @Override
        public void setInstanceFollowRedirects(boolean f) {
            super.setInstanceFollowRedirects(f);
            real.setInstanceFollowRedirects(f);
        }

        @Override
        public void setUseCaches(boolean f) {
            super.setUseCaches(f);
            real.setUseCaches(f);
        }

        @Override
        public void connect() {
            connected = true;
        }

        @Override
        public void disconnect() {
            real.disconnect();
        }

        @Override
        public boolean usingProxy() {
            return real.usingProxy();
        }

        @Override
        public OutputStream getOutputStream() throws IOException {
            return w.output();
        }

        @Override
        public InputStream getInputStream() throws IOException {
            return w.input();
        }

        @Override
        public InputStream getErrorStream() {
            return w.error();
        }

        @Override
        public int getResponseCode() throws IOException {
            return w.code();
        }

        @Override
        public String getResponseMessage() throws IOException {
            w.code();
            return "";
        }

        @Override
        public Map<String, List<String>> getHeaderFields() {
            return w.headers();
        }

        @Override
        public String getHeaderField(String name) {
            return w.header(name);
        }
    }

    private static final class Https extends HttpsURLConnection {
        private final HttpsURLConnection real;
        private final WatchedConnection w;

        Https(URL u, HttpsURLConnection real) {
            super(u);
            this.real = real;
            w = new WatchedConnection(real, this);
        }

        @Override
        public String getCipherSuite() {
            return real.getCipherSuite();
        }

        @Override
        public Certificate[] getLocalCertificates() {
            return real.getLocalCertificates();
        }

        @Override
        public Certificate[] getServerCertificates() throws javax.net.ssl.SSLPeerUnverifiedException {
            return real.getServerCertificates();
        }

        @Override
        public Principal getLocalPrincipal() {
            return real.getLocalPrincipal();
        }

        @Override
        public void setSSLSocketFactory(javax.net.ssl.SSLSocketFactory f) {
            real.setSSLSocketFactory(f);
        }

        @Override
        public void setHostnameVerifier(javax.net.ssl.HostnameVerifier v) {
            real.setHostnameVerifier(v);
        }

        @Override
        public void setRequestMethod(String m) throws ProtocolException {
            super.setRequestMethod(m);
            real.setRequestMethod(m);
        }

        @Override
        public void setRequestProperty(String k, String v) {
            super.setRequestProperty(k, v);
            real.setRequestProperty(k, v);
        }

        @Override
        public void addRequestProperty(String k, String v) {
            super.addRequestProperty(k, v);
            real.addRequestProperty(k, v);
        }

        @Override
        public void setConnectTimeout(int ms) {
            super.setConnectTimeout(ms);
            real.setConnectTimeout(ms);
        }

        @Override
        public void setReadTimeout(int ms) {
            super.setReadTimeout(ms);
            real.setReadTimeout(ms);
        }

        @Override
        public void setInstanceFollowRedirects(boolean f) {
            super.setInstanceFollowRedirects(f);
            real.setInstanceFollowRedirects(f);
        }

        @Override
        public void setUseCaches(boolean f) {
            super.setUseCaches(f);
            real.setUseCaches(f);
        }

        @Override
        public void connect() {
            connected = true;
        }

        @Override
        public void disconnect() {
            real.disconnect();
        }

        @Override
        public boolean usingProxy() {
            return real.usingProxy();
        }

        @Override
        public OutputStream getOutputStream() throws IOException {
            return w.output();
        }

        @Override
        public InputStream getInputStream() throws IOException {
            return w.input();
        }

        @Override
        public InputStream getErrorStream() {
            return w.error();
        }

        @Override
        public int getResponseCode() throws IOException {
            return w.code();
        }

        @Override
        public String getResponseMessage() throws IOException {
            w.code();
            return "";
        }

        @Override
        public Map<String, List<String>> getHeaderFields() {
            return w.headers();
        }

        @Override
        public String getHeaderField(String name) {
            return w.header(name);
        }
    }
}
