package _ras_agent;

import java.io.ByteArrayOutputStream;
import java.io.OutputStream;
import java.io.PrintStream;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/** The replay worker: runs a job's trials on the virtual clock (docs/ARCHITECTURE.md, "Replay worker"). */
public final class Verify {
    private Verify() {}

    private static Method original, hardened, reset, observe;
    private static Class<?> bodyType;
    private static String[][] deps;
    private static String fid;
    private static Class<?> hardClass;

    public static void main(String[] argv) throws Exception {
        Map<String, Object> job = Agent.object(Json.parse(Files.readString(Path.of(argv[0]))));
        Object answer;
        try {
            answer = run(job, argv[1], argv[2], argv[3]);
        } catch (Throwable e) {
            Throwable c = e instanceof ExceptionInInitializerError && e.getCause() != null ? e.getCause() : e;
            answer = Json.map("protocol", 1, "fatal", Json.map("stage", "replay", "message", trace(c)));
        }
        Files.writeString(Path.of((String) job.get("out")), Json.write(answer));
        Runtime.getRuntime().halt(0); // whatever threads the app left behind
    }

    private static String trace(Throwable e) {
        java.io.StringWriter w = new java.io.StringWriter();
        e.printStackTrace(new java.io.PrintWriter(w));
        return w.toString();
    }

    private static Map<String, Object> run(Map<String, Object> job, String owner, String name, String runtime) throws Throwable {
        Map<String, Object> k = Agent.object(job.get("constants")), meta = Agent.object(job.get("meta"));
        Agent.project = (String) job.get("project");
        Agent.cwd = System.getProperty("user.dir");
        Agent.slowS = Agent.num(k.get("slow_s"), 3);
        Agent.hangCapS = Agent.num(k.get("hang_cap_s"), 60);
        Agent.maxAttempts = (int) Agent.num(k.get("max_trial_attempts"), 50);
        Plan.outageK = (int) Agent.num(k.get("outage_k"), 3);
        if (k.get("retry_after") != null) Agent.retryAfter = String.valueOf(k.get("retry_after"));
        Agent.verify = true;
        Thread.setDefaultUncaughtExceptionHandler((t, e) -> {
            if (e instanceof ChaosHang || e instanceof ReplayMiss) return; // a helper thread of a call that is over
            System.err.print("Exception in thread \"" + t.getName() + "\" ");
            e.printStackTrace();
        });
        Switch err = new Switch();
        System.setErr(new PrintStream(err, true, StandardCharsets.UTF_8));
        System.setOut(new PrintStream(OutputStream.nullOutputStream()));

        List<Map<String, Object>> calls = new ArrayList<>();
        for (Object c : Agent.list(Agent.object(Json.parse(Files.readString(Path.of((String) job.get("cassette"))))).get("calls"))) calls.add(Agent.object(c));
        Class<?> own = Class.forName(owner);
        hardClass = Class.forName(owner + "$_Ras_" + name);
        original = find(own, "_ras_orig_" + name);
        hardened = find(hardClass, name);
        reset = Class.forName(runtime).getDeclaredMethod("resetAll");
        reset.setAccessible(true);
        fid = (String) meta.get("id");
        deps = Agent.list(meta.get("dependencies")).stream().map(d -> new String[] {
                String.valueOf(Agent.object(d).get("name")), String.valueOf(Agent.object(d).get("kind"))}).toArray(String[][]::new);
        bodyType = Class.forName(runtime + "$Body");
        observe = Class.forName(runtime).getDeclaredMethod("observe", String.class, String[][].class, bodyType);
        observe.setAccessible(true);
        restore(Agent.object(meta.getOrDefault("globals", Map.of())));

        List<Object> results = new ArrayList<>();
        for (Object o : Agent.list(job.get("trials"))) results.add(trial(Agent.object(o), calls, err));
        return Json.map("protocol", 1, "fallback", Fallback.describe(hardClass), "results", results);
    }

    /** As the enabled wrapper would call it. */
    private static Object observed(Method m, Object[] args) throws ReflectiveOperationException {
        Object body = java.lang.reflect.Proxy.newProxyInstance(bodyType.getClassLoader(), new Class<?>[] {bodyType}, (p, call, x) -> {
            try {
                return m.invoke(null, args);
            } catch (InvocationTargetException e) {
                throw e.getCause();
            }
        });
        return observe.invoke(null, fid, deps, body);
    }

    private static Method find(Class<?> c, String name) throws NoSuchMethodException {
        for (Method m : c.getDeclaredMethods())
            if (m.getName().equals(name) && java.lang.reflect.Modifier.isStatic(m.getModifiers())) {
                m.setAccessible(true);
                return m;
            }
        throw new NoSuchMethodException("static " + name + " in " + c.getName());
    }

    private static void restore(Map<String, Object> globals) throws ReflectiveOperationException {
        for (Map.Entry<String, Object> e : globals.entrySet()) {
            int at = e.getKey().lastIndexOf('#');
            try {
                java.lang.reflect.Field f = Class.forName(e.getKey().substring(0, at)).getDeclaredField(e.getKey().substring(at + 1));
                f.setAccessible(true);
                f.set(null, Codec.decode(e.getValue(), f.getGenericType()));
            } catch (NoSuchFieldException | ClassNotFoundException | Codec.Unsupported x) {
                // gone since the recording: verify's happy path will tell if it mattered
            }
        }
    }

    private static Map<String, Object> trial(Map<String, Object> spec, List<Map<String, Object>> calls, Switch err) throws Throwable {
        Map<String, Object> call = calls.get(((Number) spec.get("call")).intValue());
        boolean hard = "hardened".equals(spec.get("side"));
        Method m = hard ? hardened : original;
        if (spec.get("gap_s") == null) {
            reset.invoke(null);
            Agent.clock.clear();
        } else Agent.clock.advanceTo(Agent.clock.now() + Agent.nanos(Agent.num(spec.get("gap_s"), 0)));
        List<Object> raw = Agent.list(call.get("args"));
        Object[] args = args(m, raw), kept = args(m, raw);
        Agent.Trial t = new Agent.Trial();
        t.plan = new Plan(Agent.object(spec.get("plan")));
        for (Object x : Agent.list(call.get("io"))) t.io = plus(t.io, Agent.object(x));
        t.used = new boolean[t.io.size()];
        t.start = Agent.clock.now();
        Agent.random = new java.util.Random(((Number) spec.get("seed")).longValue());
        ByteArrayOutputStream log = new ByteArrayOutputStream();
        err.to = log;
        Object[] got = new Object[2];
        t.thread = new Thread(() -> {
            try {
                got[0] = hard ? observed(m, args) : m.invoke(null, args);
            } catch (InvocationTargetException e) {
                got[1] = e.getCause();
            } catch (Throwable e) {
                got[1] = e;
            }
        }, "ras-trial");
        t.thread.setDaemon(true);
        Agent.trial = t;
        t.thread.start();
        supervise(t);
        long end = Agent.clock.now();
        t.over = true;
        t.end.countDown();
        Thread.State st = t.thread.getState();
        boolean done = st == Thread.State.TERMINATED;
        synchronized (log) {
            err.to = null;
        }

        Throwable e = (Throwable) got[1];
        String outcome = t.hang || !done ? "hang" : t.miss != null ? "miss" : e != null ? "raise" : "return";
        String text = log.toString(StandardCharsets.UTF_8);
        Map<String, Object> r = Json.map("outcome", outcome, "latency", Math.round((end - t.start) / 1e5) / 1e4);
        Object enc = null;
        boolean encoded = true;
        if (outcome.equals("return")) {
            try {
                enc = Codec.encode(got[0]);
            } catch (Codec.Unsupported x) {
                encoded = false;
            }
        }
        boolean raises = call.containsKey("exc");
        boolean matches = outcome.equals("return") ? !raises && encoded && Fallback.same(enc, call.get("result"))
                : outcome.equals("raise") && raises && isA(e, Agent.object(call.get("exc")));
        r.put("matches", matches);
        r.put("is_fallback", hard && outcome.equals("return") && encoded && Fallback.matches(hardClass, kept, got[0]));
        r.put("logs", text.lines().filter(l -> l.contains("\"trace_id\"") || l.contains("\"TraceId\"")).count());
        List<Object> as = new ArrayList<>();
        synchronized (t.attempts) {
            for (Agent.Attempt a : t.attempts) as.add(a.json());
        }
        r.put("attempts", as);
        r.put("miss", t.miss);
        Map<?, ?> problem = e == null ? null : problem(e);
        r.put("exc", e == null ? null : clip(e.getClass().getSimpleName() + ": " + e.getMessage()));
        r.put("exc_type", e == null ? null : problem != null ? "RasError" : e.getClass().getSimpleName());
        r.put("cause_type", e == null || e.getCause() == null ? null : e.getCause().getClass().getSimpleName());
        r.put("structured", problem != null);
        if (problem != null) r.put("problem", problem);
        r.put("state", Json.map("args", digest(args)));
        r.put("result", encoded && outcome.equals("return") ? clip(Json.write(enc)) : null);
        r.put("expected", raises ? "raises " + Agent.object(call.get("exc")).get("type") : clip(Json.write(call.get("result"))));
        r.put("stderr", text.length() > 8000 ? text.substring(0, 8000) : text);
        return r;
    }

    private static List<Map<String, Object>> plus(List<Map<String, Object>> l, Map<String, Object> x) {
        List<Map<String, Object>> out = new ArrayList<>(l);
        out.add(x);
        return out;
    }

    /** Waits for the trial; if every thread of it waits, time moves on; if nothing is left to happen, it hangs. */
    private static void supervise(Agent.Trial t) throws InterruptedException {
        long deadline = System.nanoTime() + 20_000_000_000L;
        int idle = 0;
        while (true) {
            t.thread.join(idle == 0 ? 20 : 5);
            if (!t.thread.isAlive()) return;
            if (System.nanoTime() > deadline) {
                t.hang = true;
                return;
            }
            idle = Agent.quiescent() ? idle + 1 : 0;
            if (idle < 4) continue;
            if (Agent.clock.pump(Long.MAX_VALUE)) {
                idle = 1;
                if (Agent.clock.now() - t.start <= Agent.nanos(Agent.hangCapS * 20)) continue;
                t.hang = true;
                return;
            }
            if (idle > 60) {
                Agent.clock.advanceTo(Agent.clock.now() + Agent.nanos(Agent.hangCapS));
                t.hang = true;
                return;
            }
        }
    }

    private static Object[] args(Method m, List<Object> raw) {
        Object[] a = new Object[raw.size()];
        for (int i = 0; i < a.length; i++) a[i] = Codec.decode(raw.get(i), m.getGenericParameterTypes()[i]);
        return a;
    }

    private static String digest(Object[] args) {
        try {
            List<Object> l = new ArrayList<>();
            for (Object a : args) l.add(Codec.encode(a));
            return Agent.hex(Plan.sha256(Json.write(l).getBytes(StandardCharsets.UTF_8))).substring(0, 16);
        } catch (Codec.Unsupported e) {
            return "?";
        }
    }

    private static boolean isA(Throwable e, Map<String, Object> want) {
        String mod = String.valueOf(want.get("module")), n = (mod.isEmpty() ? "" : mod + ".") + want.get("type");
        for (Class<?> c = e.getClass(); c != null; c = c.getSuperclass()) if (c.getName().equals(n)) return true;
        return false;
    }

    /** problem() of the runtime's structured errors. */
    private static Map<?, ?> problem(Throwable e) {
        try {
            Method p = e.getClass().getMethod("problem");
            p.setAccessible(true);
            return p.invoke(e) instanceof Map<?, ?> m ? new TreeMap<>(m) : null;
        } catch (ReflectiveOperationException x) {
            return null;
        }
    }

    private static String clip(String s) {
        return s == null || s.length() <= 120 ? s : s.substring(0, 120);
    }

    /** System.err, collected per trial. */
    private static final class Switch extends OutputStream {
        volatile ByteArrayOutputStream to;

        @Override
        public void write(int b) {
            ByteArrayOutputStream o = to;
            if (o != null) o.write(b);
        }

        @Override
        public void write(byte[] b, int off, int len) {
            ByteArrayOutputStream o = to;
            if (o != null) o.write(b, off, len);
        }
    }
}
