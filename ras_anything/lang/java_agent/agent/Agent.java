package _ras_agent;

import java.io.IOException;
import java.net.URI;
import java.net.URL;
import java.net.URLConnection;
import java.net.http.HttpClient;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * Runs inside the instrumented app: attributes dependency calls to project functions, injects faults, records calls,
 * and (verify) serves recorded answers on a virtual clock. Without a configuration everything passes through.
 */
public final class Agent {
    private Agent() {}

    // ---------------------------------------------------------------- state

    /** One dependency attempt, as reported (docs/ARCHITECTURE.md, run output). */
    static final class Attempt {
        String key, method, idem, fault;
        int n;
        boolean nonidempotent, reached, failed = true, outage, cut;

        Map<String, Object> json() {
            return Json.map("key", key, "n", n, "method", method, "nonidempotent", nonidempotent, "idem", idem, "fault", fault,
                    "reached", reached, "failed", failed, "outage", outage);
        }
    }

    static final class Fn {
        final Map<String, Object> facts;
        final String id;
        long calls, boundary;
        final Map<String, Integer> kinds = new TreeMap<>(), faults = new TreeMap<>();

        Fn(Map<String, Object> facts) {
            this.facts = facts;
            id = (String) facts.get("id");
        }
    }

    record Range(long start, long end, int fn) {}

    /** What happens inside one recorded or benched call. */
    static final class Scope {
        final List<Map<String, Object>> io = Collections.synchronizedList(new ArrayList<>());
        final List<Attempt> attempts = Collections.synchronizedList(new ArrayList<>());
    }

    static final class Stat {
        long calls, ok, fallback, attempts, outageCalls, outageAttempts;
        boolean hardened;
        final Map<String, Integer> raised = new TreeMap<>();
        final List<Double> durs = new ArrayList<>();
    }

    /** One verify trial. */
    static final class Trial {
        Thread thread;
        volatile boolean hang, over;
        volatile String miss;
        volatile int hung;
        long start;
        Plan plan = Plan.none();
        List<Map<String, Object>> io = List.of();
        boolean[] used = {};
        final List<Attempt> attempts = Collections.synchronizedList(new ArrayList<>());
        final Map<String, Integer> counts = new HashMap<>();
        final CountDownLatch end = new CountDownLatch(1);
    }

    static final Object LOCK = new Object();
    static Map<String, Object> cfg;
    static String mode = "", project, cwd;
    static Plan plan = Plan.none();
    static double scale = 0.05, hangS = 2.0, slowS = Plan.SLOW_DEFAULT, hangCapS = 60;
    static String retryAfter = "1";
    static int maxAttempts = 50;

    static final List<Fn> fns = new ArrayList<>();
    static final Map<String, List<Range>> ranges = new HashMap<>();
    static final List<Attempt> attempts = new ArrayList<>();
    static final Map<String, Integer> counts = new HashMap<>();
    static final Map<String, Stat> stats = new TreeMap<>();
    static int hangs;
    static Map<String, Object> uncaught;
    static boolean finished;

    static final List<Map<String, Object>> recorded = new ArrayList<>();
    static int seen, skipped, maxCalls = 500;
    static Map<String, Object> globals;
    static final InheritableThreadLocal<Scope> scope = new InheritableThreadLocal<>();
    static final List<Scope> open = new ArrayList<>();

    static final VClock clock = new VClock();
    static volatile Trial trial;
    static volatile boolean verify;
    private static final Instant BASE = Instant.parse("2026-01-01T00:00:00Z");

    /** Live faults that leave a caller waiting forever, unless its own timeout fires. */
    static int hanging;
    /** A timeout of the app's own fired on such a wait. */
    static volatile boolean gaveUp;

    static {
        String path = System.getProperty("ras.anything.cfg");
        if (path != null) {
            System.clearProperty("ras.anything.cfg");
            try {
                start(object(Json.parse(Files.readString(Path.of(path)))));
            } catch (Throwable e) {
                cfg = null;
                System.err.println("ras-anything: the Java agent couldn't start: " + e);
            }
        }
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> object(Object o) {
        return (Map<String, Object>) o;
    }

    @SuppressWarnings("unchecked")
    static List<Object> list(Object o) {
        return o == null ? List.of() : (List<Object>) o;
    }

    static double num(Object o, double def) {
        return o instanceof Number n ? n.doubleValue() : def;
    }

    private static void start(Map<String, Object> c) throws IOException {
        mode = (String) c.get("mode");
        project = (String) c.get("project");
        cwd = (String) c.get("cwd");
        scale = num(c.get("scale"), 0.05);
        hangS = num(c.get("hang_s"), 2.0);
        maxCalls = (int) num(c.get("max_calls"), 500);
        if ((mode.equals("chaos") || mode.equals("bench")) && c.get("seed") != null)
            plan = new Plan(Json.map("type", "seeded", "seed", c.get("seed"), "rate", c.get("rate"), "outage_rate", c.get("outage_rate"), "salt", ""));
        Map<String, Object> table = object(Json.parse(Files.readString(Path.of((String) c.get("java_functions")))));
        for (Object f : list(table.get("functions"))) fns.add(new Fn(object(f)));
        for (Object o : list(table.get("ranges"))) {
            Map<String, Object> r = object(o);
            ranges.computeIfAbsent((String) r.get("unit"), k -> new ArrayList<>())
                    .add(new Range(((Number) r.get("start")).longValue(), ((Number) r.get("end")).longValue(), ((Number) r.get("fn")).intValue()));
        }
        cfg = c;
        Thread.setDefaultUncaughtExceptionHandler((t, e) -> {
            if (t.getName().equals("main")) noteUncaught(e);
            System.err.print("Exception in thread \"" + t.getName() + "\" ");
            e.printStackTrace();
            if (!t.getName().equals("main")) return;
            // The workload failed. Threads it left behind (a server it started) would keep the JVM up for nothing.
            finish();
            System.out.flush();
            System.err.flush();
            Runtime.getRuntime().halt(1);
        });
        Runtime.getRuntime().addShutdownHook(new Thread(Agent::finish, "ras-finish"));
    }

    // ---------------------------------------------------------------- functions and attribution

    /** First statement of every project method (inserted by the build). */
    public static void enter(int fn) {
        if (cfg == null) return;
        synchronized (LOCK) {
            fns.get(fn).calls++;
        }
    }

    private static int at(String className, String fileName, int line) {
        int dot = className.lastIndexOf('.');
        List<Range> rs = ranges.get((dot < 0 ? "" : className.substring(0, dot)) + "/" + fileName);
        int best = -1;
        long size = Long.MAX_VALUE;
        if (rs != null) for (Range r : rs) {
            if (line >= r.start && line <= r.end && r.end - r.start < size) {
                best = r.fn;
                size = r.end - r.start;
            }
        }
        return best;
    }

    /** The innermost project function on this thread's stack, or null. */
    static Fn innermost() {
        if (ranges.isEmpty()) return null;
        return StackWalker.getInstance().walk(s -> s.filter(f -> !f.getClassName().startsWith("_ras_agent."))
                .map(f -> at(f.getClassName(), f.getFileName(), f.getLineNumber())).filter(i -> i >= 0).findFirst())
                .map(fns::get).orElse(null);
    }

    private static void noteUncaught(Throwable e) {
        synchronized (LOCK) {
            if (uncaught != null) return;
            String where = null;
            for (StackTraceElement f : e.getStackTrace()) {
                int i = at(f.getClassName(), String.valueOf(f.getFileName()), f.getLineNumber());
                if (i >= 0) {
                    where = fns.get(i).id;
                    break;
                }
            }
            uncaught = Json.map("type", e.getClass().getSimpleName(), "msg", String.valueOf(e.getMessage()), "hang", e instanceof ChaosHang, "where", where);
        }
    }

    // ---------------------------------------------------------------- wrapped calls (record, bench)

    public interface Body<R> {
        R call() throws Throwable;
    }

    public interface VoidBody {
        void call() throws Throwable;
    }

    public static <E extends Throwable> void callVoid(int fn, Object[] args, VoidBody body) throws E {
        Agent.<Object, E>call(fn, args, () -> {
            body.call();
            return null;
        });
    }

    @SuppressWarnings("unchecked")
    public static <R, E extends Throwable> R call(int fn, Object[] args, Body<R> body) throws E {
        try {
            if (cfg == null) return body.call();
            if (mode.equals("record")) return record(fns.get(fn), args, body);
            if (mode.equals("bench")) return bench(fns.get(fn), args, body);
            return body.call();
        } catch (Throwable e) {
            throw (E) e;
        }
    }

    private static Scope push() {
        Scope s = new Scope();
        scope.set(s);
        synchronized (LOCK) {
            open.add(s);
        }
        return s;
    }

    private static void pop(Scope s, Scope outer) {
        scope.set(outer);
        synchronized (LOCK) {
            open.remove(s);
        }
    }

    /** The call this thread works for: its own, or the only one running (pool threads inherit nothing). */
    static Scope current() {
        Scope s = scope.get();
        synchronized (LOCK) {
            if (s != null && open.contains(s)) return s;
            return open.size() == 1 ? open.get(0) : null;
        }
    }

    private static <R> R record(Fn f, Object[] args, Body<R> body) throws Throwable {
        boolean full;
        synchronized (LOCK) {
            seen++;
            full = recorded.size() >= maxCalls;
            if (full) skipped++;
            else if (globals == null) globals = snapshot(f);
        }
        if (full) return body.call();
        List<Object> in = new ArrayList<>();
        boolean ok = true;
        try {
            for (Object a : args) in.add(Codec.encode(a));
        } catch (Codec.Unsupported e) {
            ok = false;
        }
        Scope outer = scope.get(), s = push();
        long t0 = System.nanoTime();
        Map<String, Object> call = Json.map("args", in, "kwargs", Map.of());
        R res = null;
        Throwable thrown = null;
        try {
            res = body.call();
        } catch (Throwable e) {
            thrown = e;
        }
        double dur = secs(System.nanoTime() - t0);
        pop(s, outer);
        try {
            if (thrown != null) call.put("exc", exc(thrown));
            else call.put("result", Codec.encode(res));
        } catch (Codec.Unsupported e) {
            ok = false;
        }
        call.put("dur", dur);
        call.put("io", s.io);
        synchronized (LOCK) {
            if (ok) recorded.add(call);
            else skipped++;
        }
        if (thrown != null) throw thrown;
        return res;
    }

    static double secs(long nanos) {
        return Math.round(nanos / 1e3) / 1e6;
    }

    static Map<String, Object> exc(Throwable e) {
        String n = e.getClass().getName();
        int dot = n.lastIndexOf('.');
        return Json.map("type", n.substring(dot + 1), "module", dot < 0 ? "" : n.substring(0, dot), "msg", e.getMessage() == null ? null : Codec.str(e.getMessage()));
    }

    /** Non-final static fields of the function's file that can be stored: the state main() may have set up. */
    private static Map<String, Object> snapshot(Fn f) {
        Map<String, Object> out = new TreeMap<>();
        for (Object o : list(f.facts.get("classes"))) {
            try {
                Class<?> c = Class.forName((String) o, false, ClassLoader.getSystemClassLoader());
                for (java.lang.reflect.Field x : c.getDeclaredFields()) {
                    int m = x.getModifiers();
                    if (!java.lang.reflect.Modifier.isStatic(m) || java.lang.reflect.Modifier.isFinal(m) || x.isSynthetic()) continue;
                    x.setAccessible(true);
                    try {
                        Object v = Codec.encode(x.get(null));
                        Codec.decode(v, x.getGenericType());
                        out.put(c.getName() + "#" + x.getName(), v);
                    } catch (Codec.Unsupported e) {
                        // left as the class sets it up
                    }
                }
            } catch (ReflectiveOperationException | LinkageError e) {
                // same
            }
        }
        return out;
    }

    private static <R> R bench(Fn f, Object[] args, Body<R> body) throws Throwable {
        Scope outer = scope.get(), s = push();
        long t0 = System.nanoTime();
        R res = null;
        Throwable thrown = null;
        try {
            res = body.call();
        } catch (Throwable e) {
            thrown = e;
        }
        double dur = (System.nanoTime() - t0) / 1e9;
        pop(s, outer);
        Class<?> hard = hardened(f);
        boolean active = hard != null && !"1".equals(System.getenv("RAS_ANYTHING_DISABLE"));
        boolean fell = thrown == null && active && Fallback.matches(hard, args, res) && unanswered(s.attempts);
        synchronized (LOCK) {
            Stat st = stats.computeIfAbsent(f.id, k -> new Stat());
            st.hardened = active;
            st.calls++;
            st.durs.add(dur);
            st.attempts += s.attempts.size();
            long hit = s.attempts.stream().filter(a -> a.outage).count();
            if (hit > 0) st.outageCalls++;
            st.outageAttempts += hit;
            if (thrown != null) st.raised.merge(thrown.getClass().getSimpleName(), 1, Integer::sum);
            else if (fell) st.fallback++;
            else st.ok++;
        }
        if (thrown != null) throw thrown;
        return res;
    }

    private static Class<?> hardened(Fn f) {
        try {
            return Class.forName(f.facts.get("binary") + "$_Ras_" + f.facts.get("name"));
        } catch (ClassNotFoundException | LinkageError e) {
            return null;
        }
    }

    private static boolean unanswered(List<Attempt> as) {
        for (Attempt a : as)
            if (a.failed && as.stream().noneMatch(b -> b.key.equals(a.key) && !b.failed)) return true;
        return false;
    }

    // ---------------------------------------------------------------- one dependency attempt

    /** A dependency request, whatever API makes it. */
    static final class Req {
        final String kind, api, method, url, key, idem;
        final List<String> argv;
        final byte[] body;
        final Double timeoutS;
        final boolean nonidempotent;

        Req(String kind, String api, String method, String url, String key, String idem, List<String> argv, byte[] body, Double timeoutS) {
            this.kind = kind;
            this.api = api;
            this.method = method;
            this.url = url;
            this.key = key;
            this.idem = idem;
            this.argv = argv;
            this.body = body;
            this.timeoutS = timeoutS;
            nonidempotent = List.of("POST", "PUT", "PATCH", "DELETE").contains(method);
        }

        static Req http(String api, String method, URI u, String idem, byte[] body, Double timeoutS) {
            String host = u.getHost() == null ? String.valueOf(u.getRawAuthority()) : u.getHost();
            String key = method + " " + u.getScheme() + "://" + host + (u.getRawPath() == null ? "" : u.getRawPath())
                    + (u.getRawQuery() == null ? "" : "?" + u.getRawQuery());
            if (body.length > 0) key += " #" + hex(Plan.sha256(body)).substring(0, 12);
            return new Req("http", api, method, u.toString(), key, idem, null, body, timeoutS);
        }

        static Req process(List<String> cmd) {
            List<String> argv = new ArrayList<>();
            for (String a : cmd) {
                String base = cwd == null ? null : cwd.endsWith("/") ? cwd : cwd + "/";
                if (argv.isEmpty()) argv.add(a.substring(a.replace('\\', '/').lastIndexOf('/') + 1));
                else argv.add(base != null && a.startsWith(base) ? a.substring(base.length()) : a);
            }
            return new Req("subprocess", "process", "RUN", "", "run " + String.join(" ", argv), null, argv, new byte[0], null);
        }

        Map<String, Object> json() {
            return Json.map("kind", kind, "method", method, "url", url, "body_sha", body.length == 0 ? null : hex(Plan.sha256(body)),
                    "idempotency_key", idem, "timeout", timeoutS, "argv", argv, "key", key);
        }
    }

    static String hex(byte[] b) {
        StringBuilder s = new StringBuilder();
        for (byte x : b) s.append(Character.forDigit((x >> 4) & 15, 16)).append(Character.forDigit(x & 15, 16));
        return s.toString();
    }

    /** A dependency's answer: an HTTP response, a finished process, or an exception. */
    static final class Out {
        int status;
        List<List<String>> headers = List.of();
        byte[] body = {};
        byte[] err = {};
        Exception exc;

        static Out of(int status, byte[] body, List<List<String>> headers) {
            Out o = new Out();
            o.status = status;
            o.body = body;
            o.headers = headers;
            return o;
        }

        static Out of(Exception e) {
            Out o = new Out();
            o.exc = e;
            return o;
        }

        boolean failed(String kind) {
            if (exc != null) return true;
            return kind.equals("subprocess") ? status == 75 || status >= 128 || status < 0 : status >= 500 || status == 429;
        }

        Map<String, Object> json(Req r) {
            if (exc != null) return Json.map("exc", Agent.exc(exc));
            if (r.kind.equals("subprocess")) return Json.map("returncode", status, "stdout", Codec.encode(body), "stderr", Codec.encode(err));
            return Json.map("status", status, "reason", "", "headers", headers, "body", Codec.encode(body), "url", r.url);
        }
    }

    /** What an attempt comes to: after `delay` seconds `out`, or a hang. */
    static final class Fate {
        Attempt rec;
        Req req;
        Out out;
        double delay;
        boolean hang;
    }

    interface Live {
        Out run() throws IOException, InterruptedException;
    }

    static boolean watching() {
        return cfg != null || verify;
    }

    private static Attempt openAttempt(Req r, Fn f, Plan p, Map<String, Integer> counts, List<Attempt> to) {
        Attempt a = new Attempt();
        a.key = r.key;
        a.n = counts.merge(r.key, 1, Integer::sum) - 1;
        a.method = r.method;
        a.idem = r.idem;
        a.nonidempotent = r.nonidempotent;
        a.fault = p.fault(r.key, a.n);
        a.outage = p.outage(r.key, a.n);
        to.add(a);
        if (f != null) {
            f.boundary++;
            f.kinds.merge(r.kind, 1, Integer::sum);
            if (a.fault != null) f.faults.merge(a.fault, 1, Integer::sum);
        }
        return a;
    }

    private static Out injected(Req r, String fault) {
        if (r.kind.equals("subprocess")) return Out.of(75, new byte[0], List.of());
        boolean busy = fault.equals("http429");
        return Out.of(busy ? 429 : 503, (busy ? "Too Many Requests" : "Service Unavailable").getBytes(),
                busy ? List.of(List.of("content-type", "text/plain"), List.of("retry-after", retryAfter)) : List.of(List.of("content-type", "text/plain")));
    }

    static Exception refusedError(Req r) {
        if (r.kind.equals("subprocess"))
            return new IOException("Cannot run program \"" + r.argv.get(0) + "\": error=11, Resource temporarily unavailable");
        return new java.net.ConnectException("Connection refused");
    }

    static Exception timeoutError(Req r) {
        return r.api.equals("HttpClient") ? new java.net.http.HttpTimeoutException("request timed out") : new java.net.SocketTimeoutException("Read timed out");
    }

    private static boolean cut(Out o) {
        if (o.exc != null || o.body.length == 0) return false;
        o.body = Arrays.copyOf(o.body, o.body.length / 2);
        return true;
    }

    /** A live attempt (scan, chaos, record, bench): injects the plan's fault, or runs `live`. Returns the answer. */
    static Out live(Req r, Live live) throws IOException, InterruptedException {
        Attempt a = liveAttempt(r);
        Out out = liveAnswer(r, a, live);
        close(r, a, out);
        return out;
    }

    static Attempt liveAttempt(Req r) {
        Fn f = innermost();
        Scope s = current();
        synchronized (LOCK) {
            Attempt a = openAttempt(r, f, plan, counts, attempts);
            if (s != null) s.attempts.add(a);
            return a;
        }
    }

    static void close(Req r, Attempt a, Out out) {
        a.failed = out.failed(r.kind) || a.cut;
    }

    static Out liveAnswer(Req r, Attempt a, Live live) throws IOException, InterruptedException {
        String fault = a.fault == null ? "" : a.fault;
        switch (fault) {
            case "timeout" -> {
                if (r.nonidempotent) {
                    try {
                        live.run();
                    } catch (IOException e) {
                        // only the answer is lost
                    }
                    a.reached = true;
                }
                if (r.timeoutS == null) {
                    liveHang(r);
                    return Out.of(timeoutError(r));
                }
                liveWait(r.timeoutS);
                return Out.of(timeoutError(r));
            }
            case "refused" -> {
                return Out.of(refusedError(r));
            }
            case "http5xx", "http429" -> {
                return injected(r, fault);
            }
            case "slow" -> {
                if (r.timeoutS != null && r.timeoutS < slowS) {
                    if (r.nonidempotent) {
                        live.run();
                        a.reached = true;
                    }
                    liveWait(r.timeoutS);
                    return Out.of(timeoutError(r));
                }
                liveWait(slowS);
            }
            default -> { }
        }
        Out out;
        try {
            out = live.run();
        } catch (IOException e) {
            out = Out.of(e);
        }
        a.reached = true;
        if (fault.equals("truncated")) a.cut = cut(out);
        return out;
    }

    static void liveWait(double s) throws InterruptedException {
        Thread.sleep(Math.round(1000 * Math.min(s * scale, hangS)));
    }

    /** Blocks like a dependency that never answers: an interrupt gets out; otherwise the run ends as a hang. */
    static void liveHang(Req r) throws InterruptedException {
        Fn f = innermost();
        synchronized (LOCK) {
            hanging++;
        }
        try {
            Thread.sleep(Math.round(1000 * hangS));
        } finally {
            synchronized (LOCK) {
                hanging--;
            }
        }
        if (gaveUp && !Thread.currentThread().getName().equals("main")) throw new InterruptedException(); // someone stopped waiting for this thread
        declareHang(f == null ? r.key : f.id);
    }

    static void declareHang(String where) {
        if (verify) return;
        synchronized (LOCK) {
            hangs++;
            uncaught = Json.map("type", "ChaosHang", "msg", "boundary call with no timeout hung (chaos)", "hang", true, "where", where);
        }
        finish();
        System.out.flush();
        Runtime.getRuntime().halt(70);
    }

    static void recordIo(Req r, Out out, long t0) {
        if (!"record".equals(mode)) return;
        Scope s = current();
        if (s != null) s.io.add(Json.map("req", r.json(), "out", out.json(r), "dur", secs(System.nanoTime() - t0)));
    }

    /** A verify attempt: the recorded answer of this call with the plan's fault applied. */
    static Fate replay(Req r) {
        Trial t = trial;
        Map<String, Object> match = null;
        Attempt a;
        synchronized (LOCK) {
            if (t == null || t.over) throw new ReplayMiss("a request outside the call: " + r.key);
            if (t.attempts.size() >= maxAttempts) hangNow(t, "more than " + maxAttempts + " dependency attempts in one call");
            for (int i = 0; i < t.io.size(); i++) {
                if (!r.key.equals(Codec.text(object(t.io.get(i).get("req")).get("key")))) continue;
                match = t.io.get(i);
                if (!t.used[i]) {
                    t.used[i] = true;
                    break;
                }
            }
            if (match == null) {
                t.miss = "request the original never made: " + r.key;
                throw new ReplayMiss(t.miss);
            }
            a = openAttempt(r, null, t.plan, t.counts, t.attempts);
        }
        Fate f = new Fate();
        f.rec = a;
        f.req = r;
        String fault = a.fault == null ? "" : a.fault;
        switch (fault) {
            case "timeout" -> {
                a.reached = r.nonidempotent;
                if (r.timeoutS == null) {
                    f.hang = true;
                    return f;
                }
                f.delay = r.timeoutS;
                f.out = Out.of(timeoutError(r));
            }
            case "refused" -> f.out = Out.of(refusedError(r));
            case "http5xx", "http429" -> f.out = injected(r, fault);
            default -> {
                f.out = recordedOut(r, object(match.get("out")));
                f.delay = num(match.get("dur"), 0) + (fault.equals("slow") ? slowS : 0);
                a.reached = true;
                if (r.timeoutS != null && f.delay > r.timeoutS) {
                    f.delay = r.timeoutS;
                    f.out = Out.of(timeoutError(r));
                } else if (fault.equals("truncated") && cut(f.out)) {
                    a.failed = true;
                    return f;
                }
            }
        }
        a.failed = f.out.failed(r.kind);
        return f;
    }

    private static final Map<String, java.util.function.Function<String, Exception>> EXCS = Map.ofEntries(
            Map.entry("java.net.ConnectException", java.net.ConnectException::new),
            Map.entry("java.net.SocketTimeoutException", java.net.SocketTimeoutException::new),
            Map.entry("java.net.UnknownHostException", java.net.UnknownHostException::new),
            Map.entry("java.net.SocketException", java.net.SocketException::new),
            Map.entry("java.net.ProtocolException", java.net.ProtocolException::new),
            Map.entry("java.net.http.HttpTimeoutException", java.net.http.HttpTimeoutException::new),
            Map.entry("java.net.http.HttpConnectTimeoutException", java.net.http.HttpConnectTimeoutException::new),
            Map.entry("java.io.FileNotFoundException", java.io.FileNotFoundException::new),
            Map.entry("java.io.EOFException", java.io.EOFException::new),
            Map.entry("javax.net.ssl.SSLException", javax.net.ssl.SSLException::new));

    private static Out recordedOut(Req r, Map<String, Object> o) {
        if (o.get("exc") != null) {
            Map<String, Object> e = object(o.get("exc"));
            return Out.of(EXCS.getOrDefault(e.get("module") + "." + e.get("type"), IOException::new).apply(Codec.text(e.get("msg"))));
        }
        boolean sub = r.kind.equals("subprocess");
        List<List<String>> hs = new ArrayList<>();
        for (Object h : list(o.get("headers"))) hs.add(List.of(String.valueOf(list(h).get(0)), String.valueOf(list(h).get(1))));
        Out out = Out.of((int) num(o.get(sub ? "returncode" : "status"), 0), bytes(o.get(sub ? "stdout" : "body")), hs);
        if (sub) out.err = bytes(o.get("stderr"));
        return out;
    }

    private static byte[] bytes(Object v) {
        return v instanceof String s ? s.getBytes(java.nio.charset.StandardCharsets.UTF_8) : v == null ? new byte[0] : (byte[]) Codec.decode(v, byte[].class);
    }

    // ---------------------------------------------------------------- virtual time (verify)

    static boolean onTrial() {
        Trial t = trial;
        return t != null && Thread.currentThread() == t.thread;
    }

    static void hangNow(Trial t, String where) {
        t.hang = true;
        throw new ChaosHang(where);
    }

    static long nanos(double s) {
        return Math.round(s * 1e9);
    }

    /** Waits `ns` of virtual time. */
    static void vwait(long ns) throws InterruptedException {
        Trial t = trial;
        if (t == null || ns <= 0) return;
        if (onTrial()) {
            if (Thread.interrupted()) throw new InterruptedException("sleep interrupted");
            clock.advanceTo(clock.now() + ns);
            if (clock.now() - t.start > nanos(hangCapS * 20)) hangNow(t, "the call kept running");
            return;
        }
        CountDownLatch due = new CountDownLatch(1);
        VClock.Event e = clock.schedule(clock.now() + ns, due::countDown);
        try {
            awaitOr(t, due);
        } finally {
            clock.cancel(e);
        }
    }

    private static void awaitOr(Trial t, CountDownLatch due) throws InterruptedException {
        while (!due.await(5, TimeUnit.MILLISECONDS)) if (t.over) throw new ChaosHang("the call is over");
    }

    /** Never answers. */
    static void vhang(String where) throws InterruptedException {
        Trial t = trial;
        if (t == null) throw new ChaosHang(where);
        if (onTrial()) {
            clock.advanceTo(clock.now() + nanos(hangCapS));
            hangNow(t, where);
        }
        synchronized (LOCK) {
            t.hung++;
        }
        t.end.await();
        throw new ChaosHang("the call is over");
    }

    /** No app thread but this one can make progress by itself. */
    static boolean quiescent() {
        Trial t = trial;
        Thread[] ts = new Thread[256];
        int n = Thread.currentThread().getThreadGroup().enumerate(ts, true);
        for (int i = 0; i < n; i++) {
            Thread x = ts[i];
            if (x == Thread.currentThread() || x.getName().startsWith("HttpClient-") || x.getName().startsWith("ras-ctl") || x.getName().equals("main")) continue;
            if (t != null && x == t.thread && Thread.currentThread() != t.thread && x.getState() == Thread.State.RUNNABLE) return false;
            if (x.getState() == Thread.State.RUNNABLE || x.getState() == Thread.State.NEW) return false;
        }
        return true;
    }

    /** Waits for a future on the virtual clock; deadline < 0 = forever. */
    private static <T> T vget(Future<T> f, long deadline) throws InterruptedException, ExecutionException, TimeoutException {
        Trial t = trial;
        if (!onTrial()) {
            VClock.Event e = deadline < 0 ? null : clock.schedule(deadline, () -> { });
            try {
                while (true) {
                    try {
                        return f.get(2, TimeUnit.MILLISECONDS);
                    } catch (TimeoutException x) {
                        if (f.isDone()) throw x;
                        if (t == null || t.over) throw new ChaosHang("the call is over");
                        if (deadline >= 0 && clock.now() >= deadline) throw new TimeoutException();
                    }
                }
            } finally {
                if (e != null) clock.cancel(e);
            }
        }
        int idle = 0;
        while (true) {
            if (f.isDone()) return f.get();
            try {
                return f.get(1, TimeUnit.MILLISECONDS);
            } catch (TimeoutException x) {
                if (f.isDone()) throw x;
            }
            idle = quiescent() ? idle + 1 : 0;
            if (idle < 3) continue;
            idle = 0;
            if (clock.pump(deadline < 0 ? Long.MAX_VALUE : deadline)) {
                if (clock.now() - t.start > nanos(hangCapS * 20)) hangNow(t, "the call kept running");
                continue;
            }
            if (deadline < 0) vhang("waiting for a future that never completes");
            clock.advanceTo(deadline);
            throw new TimeoutException();
        }
    }

    // ---------------------------------------------------------------- replacements the build puts into the app's code

    public static long nanoTime() {
        return verify ? 1_000_000_000_000L + clock.now() : System.nanoTime();
    }

    public static long currentTimeMillis() {
        return verify ? BASE.toEpochMilli() + clock.now() / 1_000_000 : System.currentTimeMillis();
    }

    public static Instant now() {
        return verify ? BASE.plusNanos(clock.now()) : Instant.now();
    }

    public static void sleep(long ms) throws InterruptedException {
        if (!verify) Thread.sleep(ms);
        else if (ms < 0) throw new IllegalArgumentException("timeout value is negative");
        else vwait(ms * 1_000_000);
    }

    public static void sleep(long ms, int ns) throws InterruptedException {
        sleep(ms + (ns > 0 ? 1 : 0));
    }

    public static void sleep(Duration d) throws InterruptedException {
        if (verify) vwait(d.toNanos());
        else if (!d.isNegative()) Thread.sleep(d.toMillis(), d.toNanosPart() % 1_000_000);
    }

    public static void sleep(TimeUnit u, long n) throws InterruptedException {
        if (verify) vwait(u.toNanos(n));
        else u.sleep(n);
    }

    public static void exit(int code) {
        if (verify) throw new ExitError(code);
        System.exit(code);
    }

    static final class ExitError extends Error {
        ExitError(int code) {
            super("System.exit(" + code + ")");
        }
    }

    static java.util.Random random = new java.util.Random();

    public static double random() {
        return verify ? random.nextDouble() : Math.random();
    }

    public static java.util.Random rnd() {
        return verify ? random : java.util.concurrent.ThreadLocalRandom.current();
    }

    public static java.util.Random newRandom() {
        return verify ? new java.util.Random(random.nextLong()) : new java.util.Random();
    }

    public static <T> T get(Future<T> f, long n, TimeUnit u) throws InterruptedException, ExecutionException, TimeoutException {
        if (verify) return vget(f, clock.now() + u.toNanos(n));
        if (cfg == null) return f.get(n, u);
        long end = System.nanoTime() + u.toNanos(n), cutoff = System.nanoTime() + nanos(Math.min(u.toNanos(n) / 1e9 * scale, hangS));
        while (true) { // an injected hang: the caller's timeout fires at the scaled time
            try {
                return f.get(Math.max(1, Math.min(5_000_000, end - System.nanoTime())), TimeUnit.NANOSECONDS);
            } catch (TimeoutException e) {
                if (f.isDone()) continue;
                if (System.nanoTime() < end && !(hanging > 0 && System.nanoTime() >= cutoff)) continue;
                gaveUp |= hanging > 0;
                throw e;
            }
        }
    }

    public static <T> T get(Future<T> f) throws InterruptedException, ExecutionException {
        if (!verify) return f.get();
        try {
            return vget(f, -1);
        } catch (TimeoutException e) {
            throw new IllegalStateException(e);
        }
    }

    public static <T> T join(CompletableFuture<T> f) {
        if (!verify || !onTrial()) return f.join();
        try {
            return vget(f, -1);
        } catch (ExecutionException e) {
            throw e.getCause() instanceof java.util.concurrent.CompletionException c ? c : new java.util.concurrent.CompletionException(e.getCause());
        } catch (InterruptedException | TimeoutException e) {
            throw new java.util.concurrent.CompletionException(e);
        }
    }

    public static <T> CompletableFuture<T> orTimeout(CompletableFuture<T> f, long n, TimeUnit u) {
        if (verify) {
            VClock.Event e = clock.schedule(clock.now() + u.toNanos(n), () -> f.completeExceptionally(new TimeoutException()));
            f.whenComplete((v, x) -> clock.cancel(e));
            return f;
        }
        gaveUp |= hanging > 0;
        return cfg != null && hanging > 0 ? f.orTimeout(Math.max(1, (long) (u.toMillis(n) * scale)), TimeUnit.MILLISECONDS) : f.orTimeout(n, u);
    }

    public static <T> CompletableFuture<T> completeOnTimeout(CompletableFuture<T> f, T v, long n, TimeUnit u) {
        if (verify) {
            VClock.Event e = clock.schedule(clock.now() + u.toNanos(n), () -> f.complete(v));
            f.whenComplete((r, x) -> clock.cancel(e));
            return f;
        }
        gaveUp |= hanging > 0;
        return cfg != null && hanging > 0 ? f.completeOnTimeout(v, Math.max(1, (long) (u.toMillis(n) * scale)), TimeUnit.MILLISECONDS) : f.completeOnTimeout(v, n, u);
    }

    public static HttpClient wrap(HttpClient c) {
        return !watching() || c instanceof WatchedHttpClient ? c : new WatchedHttpClient(c);
    }

    public static URLConnection openConnection(URL u) throws IOException {
        return WatchedConnection.of(u, u.openConnection());
    }

    public static URLConnection openConnection(URL u, java.net.Proxy p) throws IOException {
        return WatchedConnection.of(u, u.openConnection(p));
    }

    public static java.io.InputStream openStream(URL u) throws IOException {
        return openConnection(u).getInputStream();
    }

    public static Process start(ProcessBuilder pb) throws IOException {
        return watching() ? WatchedProcess.start(pb) : pb.start();
    }

    public static Process exec(Runtime r, String[] cmd, String[] env, java.io.File dir) throws IOException {
        if (!watching()) return r.exec(cmd, env, dir);
        ProcessBuilder pb = new ProcessBuilder(cmd).directory(dir);
        if (env != null) {
            pb.environment().clear();
            for (String e : env) if (e.indexOf('=') > 0) pb.environment().put(e.substring(0, e.indexOf('=')), e.substring(e.indexOf('=') + 1));
        }
        return WatchedProcess.start(pb);
    }

    public static Process exec(Runtime r, String[] cmd, String[] env) throws IOException {
        return exec(r, cmd, env, null);
    }

    public static Process exec(Runtime r, String[] cmd) throws IOException {
        return exec(r, cmd, null, null);
    }

    public static Process exec(Runtime r, String cmd, String[] env, java.io.File dir) throws IOException {
        if (cmd.isEmpty()) throw new IllegalArgumentException("Empty command");
        java.util.StringTokenizer st = new java.util.StringTokenizer(cmd);
        String[] argv = new String[st.countTokens()];
        for (int i = 0; st.hasMoreTokens(); i++) argv[i] = st.nextToken();
        return exec(r, argv, env, dir);
    }

    public static Process exec(Runtime r, String cmd, String[] env) throws IOException {
        return exec(r, cmd, env, null);
    }

    public static Process exec(Runtime r, String cmd) throws IOException {
        return exec(r, cmd, null, null);
    }

    // ---------------------------------------------------------------- run output

    static void finish() {
        synchronized (LOCK) {
            if (finished || cfg == null) return;
            finished = true;
            try {
                write();
            } catch (IOException | RuntimeException e) {
                System.err.println("ras-anything: couldn't write the run's data: " + e);
            }
        }
    }

    private static void write() throws IOException {
        if (mode.equals("record") && cfg.get("calls_path") != null)
            Files.writeString(Path.of((String) cfg.get("calls_path")),
                    Json.write(Json.map("format", "ras-anything-cassette", "version", 1, "lang", "java", "calls", recorded)) + "\n");
        List<Object> out = new ArrayList<>();
        for (Fn f : fns) {
            Map<String, Object> m = new LinkedHashMap<>(f.facts);
            m.put("calls", f.calls);
            m.put("boundary", f.boundary);
            m.put("kinds", f.kinds);
            m.put("gen", false);
            m.put("faults", f.faults);
            if (f.calls > 0 || f.boundary > 0) out.add(m);
        }
        Map<String, Object> st = new TreeMap<>();
        stats.forEach((id, s) -> {
            Collections.sort(s.durs);
            int n = s.durs.size();
            st.put(id, Json.map("calls", s.calls, "ok", s.ok, "fallback", s.fallback, "raised", s.raised, "hardened", s.hardened,
                    "p50", s.durs.get(n / 2), "p99", s.durs.get(Math.min(n - 1, (int) (0.99 * n))), "attempts", s.attempts,
                    "outage_calls", s.outageCalls, "outage_attempts", s.outageAttempts));
        });
        long total = fns.stream().mapToLong(f -> f.boundary).sum();
        Map<String, Object> doc = Json.map("protocol", 1, "mode", mode,
                "runtime", Json.map("name", System.getProperty("java.vm.name"), "version", System.getProperty("java.version")),
                "executable", System.getProperty("java.home") + "/bin/java", "uncaught", uncaught,
                "boundary_total", Math.max(total, attempts.size()), "functions", out,
                "attempts", attempts.stream().map(Attempt::json).toList(), "hangs", hangs, "stats", st,
                "recorded", recorded.size(), "seen", seen, "skipped", skipped,
                "snapshot", Json.map("env", Map.of(), "globals", globals == null ? Map.of() : globals));
        Path tmp = Path.of(cfg.get("out") + ".part");
        Files.writeString(tmp, Json.write(doc));
        Files.move(tmp, Path.of((String) cfg.get("out")), java.nio.file.StandardCopyOption.REPLACE_EXISTING);
    }
}
